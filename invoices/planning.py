"""Weekly tour plan generation for medical representatives.

The generator fills an MR's week from the call points in their territory,
visiting the longest-neglected ones first and spreading the load evenly across
working days. It never overwrites a plan that has already been reviewed.
"""

from datetime import timedelta

from django.utils import timezone

from .models import CallPoint, PlanVisit, WeeklyPlan

# Monday-Saturday; Sunday is not a working day for field staff here.
WORKING_DAYS = [day for day, _ in PlanVisit.DAY_CHOICES]

DEFAULT_CALLS_PER_DAY = 6


def monday_of(date):
    """The Monday on or before `date`."""
    return date - timedelta(days=date.weekday())


def current_week_start():
    return monday_of(timezone.localdate())


def last_visit_lookup(call_points):
    """Map call point id -> date of its most recent completed visit.

    One query for the whole set, rather than one per call point.
    """
    visits = (
        PlanVisit.objects.filter(call_point__in=call_points, status="done")
        .select_related("plan")
        .values("call_point_id", "plan__week_start", "day")
    )

    latest = {}

    for row in visits:
        seen = row["plan__week_start"] + timedelta(days=row["day"])
        current = latest.get(row["call_point_id"])

        if current is None or seen > current:
            latest[row["call_point_id"]] = seen

    return latest


def generate_plan(employee, week_start, calls_per_day=DEFAULT_CALLS_PER_DAY,
                  created_by=None):
    """Create or refill a draft weekly plan for one employee.

    Returns (plan, created_count). An approved or submitted plan is returned
    untouched, so a stray click cannot wipe work a manager has already signed
    off.
    """
    week_start = monday_of(week_start)

    plan, _ = WeeklyPlan.objects.get_or_create(
        employee=employee,
        week_start=week_start,
        defaults={"created_by": created_by},
    )

    if not plan.is_editable:
        return plan, 0

    if employee.territory is None:
        return plan, 0

    call_points = list(
        CallPoint.objects.filter(territory=employee.territory, is_active=True)
    )

    if not call_points:
        return plan, 0

    latest = last_visit_lookup(call_points)

    # Never visited first, then longest since the last visit.
    never_visited = week_start - timedelta(days=3650)
    call_points.sort(key=lambda cp: (latest.get(cp.id) or never_visited, cp.name))

    plan.visits.all().delete()

    capacity = calls_per_day * len(WORKING_DAYS)
    scheduled = call_points[:capacity]

    created = 0

    for index, call_point in enumerate(scheduled):
        # Round-robin across days so every day gets a full, even round.
        day = WORKING_DAYS[index % len(WORKING_DAYS)]

        PlanVisit.objects.create(
            plan=plan,
            call_point=call_point,
            day=day,
            objective="Routine call",
        )

        created += 1

    return plan, created
