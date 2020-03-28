from django.db import models
from django.core.exceptions import ValidationError
from django.db.models.query import QuerySet
from operator import itemgetter
import datetime
from django.contrib.auth.hashers import make_password
from django.utils.text import slugify
import logging
from django.contrib.auth.models import User
from django.utils import timezone
import calendar
import random
import time


def date_to_epoch_week(date: datetime.date) -> int:
    epoch_seconds = calendar.timegm(date.timetuple())
    return int(((epoch_seconds / 60 / 60 / 24) + 3) / 7)


def epoch_week_to_monday(week: int) -> datetime.date:
    epoch_seconds = ((week * 7) - 3) * 24 * 60 * 60
    return datetime.date.fromtimestamp(time.mktime(time.gmtime(epoch_seconds)))


def epoch_week_to_sunday(week: int) -> datetime.date:
    epoch_seconds = ((week * 7) + 3) * 24 * 60 * 60
    return datetime.date.fromtimestamp(time.mktime(time.gmtime(epoch_seconds)))


def current_epoch_week():
    return date_to_epoch_week(datetime.date.today())


class ScheduleQuerySet(models.QuerySet):
    def enabled(self):
        return self.filter(disabled=False)

    def disabled(self):
        return self.filter(disabled=True)


class Schedule(models.Model):
    class Meta:
        ordering = ('cleaners_per_date',)
    name = models.CharField(max_length=20, unique=True)
    slug = models.CharField(max_length=20, unique=True)

    CLEANERS_PER_DATE_CHOICES = ((1, 'Einen'), (2, 'Zwei'))
    cleaners_per_date = models.IntegerField(default=1, choices=CLEANERS_PER_DATE_CHOICES)

    WEEKDAYS = ((0, 'Montag'), (1, 'Dienstag'), (2, 'Mittwoch'), (3, 'Donnerstag'),
                (4, 'Freitag'), (5, 'Samstag'), (6, 'Sonntag'))
    weekday = models.IntegerField(default=6, choices=WEEKDAYS)

    FREQUENCY_CHOICES = ((1, 'Jede Woche'), (2, 'Gerade Wochen'), (3, 'Ungerade Wochen'))
    frequency = models.IntegerField(default=1, choices=FREQUENCY_CHOICES)

    disabled = models.BooleanField(default=False)

    objects = ScheduleQuerySet.as_manager()

    def __str__(self) -> str:
        return self.name

    def __init__(self, *args, **kwargs):
        super(Schedule, self).__init__(*args, **kwargs)
        self.previous_cleaners_per_date = self.cleaners_per_date
        self.previous_frequency = self.frequency

    def weekday_as_name(self):
        return Schedule.WEEKDAYS[self.weekday][1]

    def constant_affiliation_timespan(self, week: int) -> dict:
        """
        Find minimal timespan during which all currently active Affiliations exist at the same time

        :param week: Epoch week number
        :return: dict with keys 'beginning' and 'end'
        """
        minimal_week_set = {}

        active_affiliations = Affiliation.objects.active_in_week_for_schedule(week, self)
        if active_affiliations.exists():

            for affiliation in active_affiliations:
                if not minimal_week_set:
                    minimal_week_set = {'beginning': affiliation.beginning, 'end': affiliation.end}
                else:
                    if affiliation.beginning > minimal_week_set['beginning']:
                        minimal_week_set['beginning'] = affiliation.beginning
                    if affiliation.end < minimal_week_set['end']:
                        minimal_week_set['end'] = affiliation.end

        return minimal_week_set

    def deployment_ratios(self, week: int) -> list:
        """week must be a epoch week number as returned by date_to_epoch_week()"""
        ratios = []

        minimal_week_set = self.constant_affiliation_timespan(week=week)

        active_affiliations = Affiliation.objects.active_in_week_for_schedule(week, self)
        if active_affiliations.exists():
            for affiliation in active_affiliations:
                cleaner = affiliation.cleaner
                ratios.append([cleaner,
                               cleaner.deployment_ratio(schedule=self,
                                                        from_week=minimal_week_set['beginning'],
                                                        to_week=minimal_week_set['end'])])
            return sorted(ratios, key=itemgetter(1), reverse=False)
        else:
            return []

    def occurs_in_week(self, week: int) -> bool:
        return self.frequency == 1 or \
               self.frequency == 2 and week % 2 == 0 or \
               self.frequency == 3 and week % 2 == 1

    def create_assignments_over_timespan(self, start_week: int, end_week: int) -> None:
        """
        Calls create_assignment() for every week between (and including) start_week to end_week

        :param start_week: First week number on which a new Assignment will be created.
        :param end_week: Last week number on which a new Assignment will be created
        :return: None
        """
        min_week = min(start_week, end_week)
        max_week = max(start_week, end_week)

        for week in range(min_week, max_week + 1):
            while self.create_assignment(week=week):
                # This loop enables Schedules with cleaners_per_date > 1 to be handled correctly, as each
                # call to create_assignment only assigns one Cleaner
                pass

    def create_assignment(self, week: int):
        """
        On a given epoch week, create Assignments for CleaningWeeks where there are
        ones to be created and recreate Assignments in CleaningWeeks where cleaning_week.assignments_valid==False.
        All while updating Tasks where cleaning_week.tasks_valid==False.

        :param week: Epoch week number to update Assignments and Tasks on
        :return: True if Assignment was created, else False
        """
        logging.debug('------------- CREATING NEW CLEANING DUTY FOR {} in week {} -------------'
                      .format(self.name, week))

        if self.disabled:
            logging.debug("NO ASSIGNMENT CREATED: This schedule is disabled!")
            return False

        if not self.occurs_in_week(week):
            cleaning_week_where_there_shouldnt_be_one = self.cleaningweek_set.filter(week=week)
            if cleaning_week_where_there_shouldnt_be_one.exists():
                cleaning_week_where_there_shouldnt_be_one.first().delete()
                logging.debug("CLEANING_WEEK DELETED: Found a CleaningWeek where the Schedule doesn't occur. ")

            logging.debug("NO ASSIGNMENT CREATED [Code01]: This schedule does not occur "
                          "in week {} as frequency is set to {}".
                          format(week, self.frequency))
            return False

        cleaning_week, was_created = self.cleaningweek_set.get_or_create(week=week)
        if not cleaning_week.tasks_valid:
            cleaning_week.create_missing_tasks()
        if not cleaning_week.assignments_valid:
            cleaning_week.assignment_set.all().delete()
            cleaning_week.set_assignments_valid_field(True)

        if cleaning_week.assignment_set.count() >= self.cleaners_per_date:
            logging.debug("NO ASSIGNMENT CREATED [Code02]: There are no open cleaning positions for week {}. "
                          "All ({}/{}) positions have been filled.".
                          format(week, self.cleaners_per_date, self.cleaners_per_date))
            return False

        ratios = self.deployment_ratios(week)
        if not ratios:
            logging.debug("NO ASSIGNMENT CREATED [Code03]: Deployment ratios are not defined for this date.")
            return False

        if logging.getLogger(__name__).getEffectiveLevel() >= logging.DEBUG:
            logging_text = "All cleaners' ratios: "
            for cleaner, ratio in ratios:
                logging_text += "{}: {}".format(cleaner.name, round(ratio, 3)) + "  "
            logging.debug(logging_text)

        # First, group by deployment_ratios
        distinct_ratio_values = list(set(x[1] for x in ratios))
        distinct_ratio_values.sort()
        grouped_by_ratios = [[x[0] for x in ratios if x[1] == val] for val in distinct_ratio_values]
        for same_ratio in grouped_by_ratios:
            logging.debug("> [{}] have the same ratio.".format(','.join([x.name for x in same_ratio])))
            non_excluded = [x for x in same_ratio if x not in cleaning_week.excluded.all()]
            logging.debug(">>  [{}] of these are NOT excluded.".format(','.join([x.name for x in non_excluded])))

            # Now, group by assignment count, so we don't randomly choose the Cleaner who has twice as many
            # Assignments in this week as the others with the same deployment_ratio.
            nr_assignments = list(set(x.nr_assignments_in_week(week) for x in same_ratio))
            nr_assignments.sort()
            grouped_by_assignment_count = [[x for x in non_excluded if x.nr_assignments_in_week(week) == count]
                                           for count in nr_assignments]

            for same_assignment_count in grouped_by_assignment_count:
                logging.debug(">>>   [{}] have the same assignment count.".format(
                    ','.join([x.name for x in same_assignment_count])))
                choice = random.choice(same_assignment_count)
                logging.debug(">>>>    {} chosen! [Code21] ----".format(choice.name))
                return self.assignment_set.create(cleaner=choice, cleaning_week=cleaning_week)

        else:
            logging.debug("---- All Cleaners are excluded or are already cleaning as much as they like. "
                          "We must choose {} [Code22]"
                          .format(ratios[0][0]))
            return self.assignment_set.create(cleaner=ratios[0][0], cleaning_week=cleaning_week)

    def save(self, force_insert=False, force_update=False, using=None, update_fields=None):
        self.slug = slugify(self.name)
        super(Schedule, self).save(force_insert, force_update, using, update_fields)

        if self.previous_frequency != self.frequency \
                or self.previous_cleaners_per_date != self.cleaners_per_date:
            [x.set_assignments_valid_field(False)
             for x in self.cleaningweek_set.filter(week__gt=current_epoch_week()).all()]


class ScheduleGroupQuerySet(models.QuerySet):
    def enabled(self):
        return self.filter(disabled=False)

    def disabled(self):
        return self.filter(disabled=True)


class ScheduleGroup(models.Model):
    """
    A ScheduleGroup can be thought of as the floor you're living on. It simplifies grouping of Cleaners.

    Cleaners can be affiliated to different ScheduleGroups over time, which is tracked by the Affiliation model.
    A ScheduleGroup should not be deleted (rather disabled), as this causes Affiliations to be deleted,
    creating holes in the affiliation history which can lead to issues in consistency and the assigning of
    duties to Cleaners.
    """
    class Meta:
        ordering = ("name", )
    name = models.CharField(max_length=30, unique=True)
    schedules = models.ManyToManyField(Schedule)
    disabled = models.BooleanField(default=False)

    objects = ScheduleGroupQuerySet.as_manager()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.__disabled = self.disabled

    def __str__(self):
        return self.name


class CleanerQuerySet(models.QuerySet):
    def active(self) -> QuerySet:
        return self.filter(
            affiliation__beginning__lte=current_epoch_week(), affiliation__end__gte=current_epoch_week())

    def inactive(self) -> QuerySet:
        return self.exclude(
            affiliation__beginning__lte=current_epoch_week(), affiliation__end__gte=current_epoch_week())


class Cleaner(models.Model):
    """
    The Cleaner is the representation of the physical person living in the house.

    Each Cleaner is linked to a User object
    """
    class Meta:
        ordering = ('name',)
    user = models.ForeignKey(User, on_delete=models.CASCADE, null=True)
    name = models.CharField(max_length=20, unique=True)
    slug = models.CharField(max_length=20, unique=True)

    time_zone = models.CharField(max_length=30, default="Europe/Berlin")

    objects = CleanerQuerySet.as_manager()

    def __str__(self):
        return self.name

    def switchable_assignments_for_request(self, duty_switch, look_weeks_into_future=12):
        if duty_switch.requester_assignment.has_passed():
            return Assignment.objects.none()

        from_week = current_epoch_week()
        schedule_from_request = duty_switch.requester_assignment.schedule
        week_from_request = duty_switch.requester_assignment.cleaning_week.week

        own_affiliation_during_request_week = self.affiliation_in_week(week_from_request)
        if own_affiliation_during_request_week is None or \
                schedule_from_request not in own_affiliation_during_request_week.group.schedules.all():
            return Assignment.objects.none()

        switchable_assignments = self.assignment_set.\
            filter(cleaning_week__week__range=(from_week, from_week+look_weeks_into_future))

        requested = duty_switch.possible_acceptors()
        acceptable = switchable_assignments
        return requested & acceptable

    def can_accept_duty_switch_request(self, duty_switch) -> bool:
        return len(self.switchable_assignments_for_request(duty_switch)) != 0

    def affiliation_in_week(self, week):
        try:
            current_affiliation = self.affiliation_set.get(
                beginning__lte=week, end__gte=week
            )
            return current_affiliation
        except Affiliation.DoesNotExist:
            return None

    def current_affiliation(self):
        return self.affiliation_in_week(current_epoch_week())

    def deployment_ratio(self, schedule: Schedule, from_week: int, to_week: int) -> float:
        all_assignments = Assignment.objects.filter(
                cleaning_week__week__gte=from_week, cleaning_week__week__lte=to_week,
                cleaning_week__schedule=schedule)

        all_assignment_count = all_assignments.count()
        own_assignment_count = all_assignments.filter(cleaner=self).count()

        if all_assignment_count != 0:
            return own_assignment_count / all_assignment_count
        else:
            return 0.0

    def is_active(self):
        return self.current_affiliation() is not None

    def nr_assignments_in_week(self, week: int):
        return self.assignment_set.filter(cleaning_week__week=week).count()

    def assignment_in_cleaning_week(self, cleaning_week):
        query = self.assignment_set.filter(cleaning_week__pk=cleaning_week.pk)
        if query.exists():
            return query.first()
        else:
            return None

    def delete(self, using=None, keep_parents=False):
        super().delete(using, keep_parents)
        self.user.delete()

    def save(self, force_insert=False, force_update=False, using=None,
             update_fields=None):
        self.slug = slugify(self.name)

        if not self.user:
            self.user = User.objects.create(username=self.slug, password=make_password(self.slug))
        elif self.user.username != self.slug:
            self.user.username = self.slug
            self.user.set_password(self.slug)
            self.user.save()

        super().save(force_insert, force_update, using, update_fields)


class AffiliationQuerySet(models.QuerySet):
    def active_in_week(self, week):
        return self.filter(beginning__lte=week, end__gte=week)

    def active_in_week_for_schedule(self, week, schedule):
        return self.active_in_week(week).filter(group__schedules=schedule)


class Affiliation(models.Model):
    """
    The Affiliation model tracks Cleaners' belonging to Schedules over time.

    The Affiliation model meant to be a chain of time periods for each Cleaner with no two Affiliations of a Cleaner
    overlapping in time. This constraint is enforced by the model.
    """
    class Meta:
        ordering = ('-end',)
        unique_together = ('beginning', 'cleaner')

    cleaner = models.ForeignKey(Cleaner, on_delete=models.CASCADE, editable=False)
    group = models.ForeignKey(ScheduleGroup, on_delete=models.CASCADE, null=False)

    beginning = models.IntegerField()
    end = models.IntegerField()

    objects = AffiliationQuerySet.as_manager()

    def __str__(self):
        return "Cleaner {} is affiliated with ScheduleGroup {} from {} (week nr. {}) to {} (week nr. {})".format(
            self.cleaner.name, self.group.name, self.beginning_as_date().strftime("%d. %b %Y"),
            self.beginning, self.end_as_date().strftime("%d. %b %Y"), self.end
        )

    def __init__(self, *args, **kwargs):
        super(Affiliation, self).__init__(*args, **kwargs)

        self.previous_beginning = self.beginning
        self.previous_end = self.end

        if self.pk:
            self.__previous_cleaner = self.cleaner
            self.__previous_group = self.group
        else:
            self.__previous_cleaner = None
            self.__previous_group = None

    def beginning_as_date(self):
        return epoch_week_to_monday(self.beginning)

    def end_as_date(self):
        return epoch_week_to_sunday(self.end)

    @staticmethod
    def date_validator(affiliation_pk, cleaner: Cleaner, beginning: int, end: int):
        if beginning > end:
            raise ValidationError("Das Ende einer Zugehörigkeit darf nicht vor dem Anfang liegen!")

        other_affiliations = cleaner.affiliation_set

        if affiliation_pk:
            other_affiliations = other_affiliations.exclude(pk=affiliation_pk)

        if other_affiliations.filter(beginning__range=(beginning, end)).exists():
            raise ValidationError("Der vogeschlagene Beginn dieser Zugehörigkeit überlappt "
                                  "mit einer anderen Zugehörigkeit. "
                                  "Bitte passe die andere Zugehörigkeit zuerst an, "
                                  "damit es zu keiner Überlappung kommt.")

        if other_affiliations.filter(end__range=(beginning, end)).exists():
            raise ValidationError("Das vogeschlagene Ende dieser Zugehörigkeit überlappt "
                                  "mit einer anderen Zugehörigkeit. "
                                  "Bitte passe die andere Zugehörigkeit zuerst an, "
                                  "damit es zu keiner Überlappung kommt.")

    def save(self, force_insert=False, force_update=False, using=None, update_fields=None):
        self.date_validator(affiliation_pk=self.pk, cleaner=self.cleaner, beginning=self.beginning, end=self.end)

        if self.previous_beginning != self.beginning or self.previous_end != self.end:
            min_beginning = min(self.previous_beginning, self.beginning)
            max_beginning = max(self.previous_beginning, self.beginning)
            beginning_affects = CleaningWeek.objects.\
                filter(week__range=(min_beginning, max_beginning)).\
                exclude(week=max_beginning)

            min_end = min(self.previous_end, self.end)
            max_end = max(self.previous_end, self.end)
            end_affects = CleaningWeek.objects.\
                filter(week__range=(min_end, max_end)).\
                exclude(week=min_end)

            # XORing both sets deals with the case that the old and new affiliation week ranges don't overlap
            cleaning_weeks_invalidated = set(beginning_affects) ^ set(end_affects)

            for cleaning_week in cleaning_weeks_invalidated:
                cleaning_week.set_assignments_valid_field(False)

        super().save(force_insert, force_update, using, update_fields)


class CleaningWeekQuerySet(models.QuerySet):
    def enabled(self):
        return self.filter(disabled=False)

    def disabled(self):
        return self.filter(disabled=True)


class CleaningWeek(models.Model):
    class Meta:
        ordering = ('-week',)
        unique_together = ('week', 'schedule')
    week = models.IntegerField(editable=False)
    excluded = models.ManyToManyField(Cleaner)
    schedule = models.ForeignKey(Schedule, on_delete=models.CASCADE, editable=False)
    tasks_valid = models.BooleanField(default=False)
    assignments_valid = models.BooleanField(default=False)
    disabled = models.BooleanField(default=False)

    objects = CleaningWeekQuerySet.as_manager()

    def __str__(self):
        return "CleaningWeek in Schedule {}, week nr. {} ({} to {})".format(
            self.schedule.name, self.week,
            self.week_start().strftime("%d. %b %Y"), self.week_end().strftime("%d. %b %Y"))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def assignment_date(self) -> datetime.date:
        return epoch_week_to_monday(self.week) + datetime.timedelta(days=self.schedule.weekday)

    def is_current_week(self) -> bool:
        return current_epoch_week() == self.week

    def tasks_are_ready_to_be_done(self):
        for task in self.task_set.all():
            if task.my_time_has_come() and task.cleaned_by is None:
                return True
        return False

    def create_missing_tasks(self):
        missing_task_templates = self.task_templates_missing()
        for task_template in missing_task_templates.enabled():
            self.task_set.create(template=task_template)
        self.tasks_valid = True
        self.save()

    def completed_tasks(self) -> QuerySet:
        return self.task_set.exclude(cleaned_by__isnull=True)

    def completed_tasks__as_templates(self) -> list:
        return [x.template for x in self.completed_tasks()]

    def open_tasks(self) -> QuerySet:
        return self.task_set.filter(cleaned_by__isnull=True)

    def open_tasks__as_templates(self) -> QuerySet:
        return TaskTemplate.objects.filter(pk__in=[x.template.pk for x in self.open_tasks()])

    def ratio_of_completed_tasks(self) -> float:
        return self.completed_tasks().count() / self.task_set.count()

    def all_tasks_are_completed(self):
        return self.ratio_of_completed_tasks() == 1.0

    def assigned_cleaners(self) -> QuerySet:
        return Cleaner.objects.filter(pk__in=[x.cleaner.pk for x in self.assignment_set.all()])

    def is_in_future(self) -> bool:
        return current_epoch_week() < self.week

    def task_templates_missing(self):
        return self.schedule.tasktemplate_set.enabled().exclude(pk__in=[x.template.pk for x in self.task_set.all()])

    def week_start(self):
        return epoch_week_to_monday(self.week)

    def week_end(self):
        return epoch_week_to_sunday(self.week)

    def set_assignments_valid_field(self, value: bool) -> None:
        self.assignments_valid = value
        self.save()

    def set_tasks_valid_field(self, value: bool) -> None:
        self.tasks_valid = value
        self.save()


class AssignmentQuerySet(models.QuerySet):
    def eligible_switching_destination_candidates(self, source_assignment):
        return self.filter(schedule=source_assignment.schedule).\
            filter(cleaning_week__week__gt=source_assignment.cleaning_week.week).\
            exclude(cleaning_week__excluded=source_assignment.cleaner)


class Assignment(models.Model):
    cleaner = models.ForeignKey(Cleaner, on_delete=models.CASCADE)
    cleaners_comment = models.CharField(max_length=200)
    created = models.DateField(auto_now_add=timezone.now().date(), editable=False)

    cleaning_week = models.ForeignKey(CleaningWeek, on_delete=models.CASCADE, editable=False)
    schedule = models.ForeignKey(Schedule, on_delete=models.CASCADE, editable=False)

    objects = AssignmentQuerySet.as_manager()

    class Meta:
        ordering = ('-cleaning_week__week',)

    def __str__(self):
        return "{}: {}, {} ".format(
            self.cleaning_week.schedule.name, self.cleaner.name, self.assignment_date().strftime('%d. %b %Y'))

    def assignment_date(self):
        return self.cleaning_week.assignment_date()

    def tasks_are_ready_to_be_done(self):
        return self.cleaning_week.tasks_are_ready_to_be_done()

    def has_passed(self):
        # We check if tasks are not ready to be done because while the assignment_date may be in the past,
        # the Tasks can possibly be done a few days after that date
        return self.assignment_date() < timezone.now().date() and not self.tasks_are_ready_to_be_done()

    def all_cleaners_in_week_for_schedule(self):
        return Cleaner.objects.filter(assignment__cleaning_week=self.cleaning_week)

    def other_cleaners_in_week_for_schedule(self):
        return self.all_cleaners_in_week_for_schedule().exclude(pk=self.cleaner.pk)

    def switch_requested(self):
        duty_switch = DutySwitch.objects.filter(requester_assignment=self).filter(acceptor_assignment__isnull=True)
        if duty_switch.exists():
            return duty_switch.first()
        else:
            return None


class TaskTemplateQuerySet(models.QuerySet):
    def enabled(self):
        return self.filter(task_disabled=False)

    def disabled(self):
        return self.filter(task_disabled=True)


class TaskTemplate(models.Model):
    task_name = models.CharField(max_length=40)
    task_help_text = models.CharField(max_length=200, default="", null=True)
    start_days_before = models.IntegerField(choices=[(0, ''), (1, ''), (2, ''), (3, ''), (4, ''), (5, ''), (6, '')])
    end_days_after = models.IntegerField(choices=[(0, ''), (1, ''), (2, ''), (3, ''), (4, ''), (5, ''), (6, '')])

    schedule = models.ForeignKey(Schedule, on_delete=models.CASCADE, editable=False)
    task_disabled = models.BooleanField(default=False)

    objects = TaskTemplateQuerySet.as_manager()

    def __init__(self, *args, **kwargs):
        super(TaskTemplate, self).__init__(*args, **kwargs)
        self.previous_disabled = self.task_disabled

    def __str__(self):
        return self.task_name

    def start_day_to_weekday(self):
        return Schedule.WEEKDAYS[(self.schedule.weekday-self.start_days_before) % 7][1]

    def end_day_to_weekday(self):
        return Schedule.WEEKDAYS[(self.schedule.weekday+self.end_days_after) % 7][1]

    def save(self, force_insert=False, force_update=False, using=None,
             update_fields=None):
        super().save(force_insert, force_update, using, update_fields)
        if self.previous_disabled != self.task_disabled:
            [x.set_tasks_valid_field(False)
             for x in self.schedule.cleaningweek_set.filter(week__gt=current_epoch_week()).all()]


class TaskQuerySet(models.QuerySet):
    def cleaned(self):
        return self.exclude(cleaned_by__isnull=True)

    def uncleaned(self):
        return self.filter(cleaned_by__isnull=True)


class Task(models.Model):
    cleaning_week = models.ForeignKey(CleaningWeek, on_delete=models.CASCADE, editable=False)
    cleaned_by = models.ForeignKey(Cleaner, null=True, on_delete=models.PROTECT)
    template = models.ForeignKey(TaskTemplate, on_delete=models.CASCADE, editable=False)

    objects = TaskQuerySet.as_manager()

    def __str__(self):
        return self.template.task_name

    def start_date(self):
        return self.cleaning_week.week_start() \
               + datetime.timedelta(days=self.cleaning_week.schedule.weekday) \
               - datetime.timedelta(days=self.template.start_days_before)

    def end_date(self):
        return self.cleaning_week.week_start() \
               + datetime.timedelta(days=self.cleaning_week.schedule.weekday) \
               + datetime.timedelta(days=self.template.end_days_after)

    def my_time_has_come(self):
        return self.start_date() <= timezone.now().date() <= self.end_date()

    def has_passed(self):
        return self.end_date() < timezone.now().date()

    def possible_cleaners(self):
        return self.cleaning_week.assigned_cleaners()

    def set_cleaned_by(self, cleaner: Cleaner):
        self.cleaned_by = cleaner
        self.save()


class DutySwitchQuerySet(models.QuerySet):
    def open(self, schedule=None):
        if schedule:
            return self.filter(acceptor_assignment__isnull=True).filter(requester_assignment__schedule=schedule)
        else:
            return self.filter(acceptor_assignment__isnull=True)

    def closed(self, schedule=None):
        if schedule:
            return self.exclude(acceptor_assignment__isnull=True).filter(requester_assignment__schedule=schedule)
        else:
            return self.exclude(acceptor_assignment__isnull=True)


class DutySwitch(models.Model):
    class Meta:
        ordering = ('created',)
    created = models.DateField(auto_now_add=timezone.now().date())

    requester_assignment = models.ForeignKey(Assignment, on_delete=models.CASCADE, related_name="requester",
                                             editable=False)
    acceptor_assignment = models.ForeignKey(Assignment, on_delete=models.SET_NULL, null=True, related_name="acceptor")

    objects = DutySwitchQuerySet.as_manager()

    def __init__(self, *args, **kwargs):
        super(DutySwitch, self).__init__(*args, **kwargs)
        self.__previous_acceptor = self.acceptor_assignment

    def __str__(self):
        if self.acceptor_assignment:
            return "Requester: {} on {}  -  Acceptor: {} on {}".\
                format(self.requester_assignment.cleaner.name,
                       self.requester_assignment.assignment_date().strftime('%d. %b %Y'),
                       self.acceptor_assignment.cleaner.name,
                       self.acceptor_assignment.assignment_date().strftime('%d. %b %Y'))
        else:
            return "Requester: {} on {}  -  Acceptor: none yet". \
                format(self.requester_assignment.cleaner.name,
                       self.requester_assignment.assignment_date().strftime('%d. %b %Y'))

    def possible_acceptors(self):
        return Assignment.objects.eligible_switching_destination_candidates(self.requester_assignment).\
            exclude(cleaner=self.requester_assignment.cleaner)

    def save(self, force_insert=False, force_update=False, using=None,
             update_fields=None):
        if self.__previous_acceptor is None and self.acceptor_assignment is not None:
            self.requester_assignment.cleaning_week.excluded.add(self.requester_assignment.cleaner)

            source_cleaner = self.requester_assignment.cleaner

            self.requester_assignment.cleaner = self.acceptor_assignment.cleaner
            self.requester_assignment.save()

            self.acceptor_assignment.cleaner = source_cleaner
            self.acceptor_assignment.save()

        super().save(force_insert, force_update, using, update_fields)
