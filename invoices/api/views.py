"""The API the Flutter app talks to.

Two rules run through all of it.

Everything is scoped to the token's own Employee. The app never sends an
employee id and the server never reads one - a stolen or shared token can only
ever reach one MR's work.

Every write is idempotent on a `client_uuid` the device generates before the
row leaves the phone. Offline-first means the same visit will be sent twice
sooner or later - a dropped reply, a retried queue, an app killed mid-sync -
and the second send has to be a no-op rather than a duplicate doctor call.
"""

from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_date

from rest_framework import status
from rest_framework.authtoken.models import Token
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from ..models import (
    CallPoint,
    CallReport,
    Doctor,
    Employee,
    Expense,
    ExpenseCategory,
    PlanVisit,
    Product,
    Target,
    Territory,
    WeeklyPlan,
    ZERO,
    field_employee,
)
from ..planning import current_week_start, generate_plan, monday_of
from ..views import month_range
from .serializers import (
    CallPointSerializer,
    CallReportSerializer,
    DoctorSerializer,
    EmployeeSerializer,
    ExpenseCategorySerializer,
    ExpenseSerializer,
    ProductSerializer,
    TargetSerializer,
    TerritorySerializer,
    WeeklyPlanSerializer,
)


def me(request):
    """The Employee behind this token, or None.

    Field logins resolve through the role; an office login that happens to be
    a team member resolves too, so a manager can use the app for their own
    calls without a second account.
    """
    return (
        field_employee(request.user)
        or Employee.objects.filter(user=request.user).first()
    )


def no_employee():
    return Response(
        {"detail": "This login is not linked to a team member."},
        status=status.HTTP_403_FORBIDDEN,
    )


def in_territory(employee):
    """Call points the MR is allowed to see and file against.

    An MR with no territory set sees everything rather than nothing: an empty
    app they cannot work from is worse than a wide one.
    """
    call_points = CallPoint.objects.filter(is_active=True)

    if employee.territory_id is not None:
        call_points = call_points.filter(territory_id=employee.territory_id)

    return call_points


# ------------------------------------------------------------------ AUTH

@api_view(["POST"])
@permission_classes([])
def login(request):
    from django.contrib.auth import authenticate

    user = authenticate(
        request,
        username=request.data.get("username", ""),
        password=request.data.get("password", ""),
    )

    if user is None:
        return Response(
            {"detail": "Wrong username or password."},
            status=status.HTTP_401_UNAUTHORIZED,
        )

    employee = (
        field_employee(user) or Employee.objects.filter(user=user).first()
    )

    if employee is None:
        return Response(
            {"detail": "This login is not linked to a team member, so the "
                       "app has nothing to show. Ask the office to link it."},
            status=status.HTTP_403_FORBIDDEN,
        )

    token, _ = Token.objects.get_or_create(user=user)

    return Response({
        "token": token.key,
        "employee": EmployeeSerializer(employee).data,
    })


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def logout(request):
    Token.objects.filter(user=request.user).delete()

    return Response({"detail": "Signed out."})


# ------------------------------------------------------------- THE CACHE

@api_view(["GET"])
@permission_classes([IsAuthenticated])
def bootstrap(request):
    """Everything the phone keeps locally so it can work with no signal.

    Small on purpose: one territory's call points and doctors, the product
    list, and the expense categories. Sending the company's whole book would
    make the first sync unusable on a slow connection.
    """
    employee = me(request)

    if employee is None:
        return no_employee()

    call_points = in_territory(employee).select_related(
        "territory"
    ).prefetch_related("doctors")

    return Response({
        "synced_at": timezone.now().isoformat(),
        "employee": EmployeeSerializer(employee).data,
        "territories": TerritorySerializer(
            Territory.objects.filter(is_active=True), many=True
        ).data,
        "call_points": CallPointSerializer(call_points, many=True).data,
        "products": ProductSerializer(
            Product.objects.filter(is_active=True), many=True
        ).data,
        "expense_categories": ExpenseCategorySerializer(
            ExpenseCategory.objects.filter(is_active=True), many=True
        ).data,
    })


# ------------------------------------------------------------- SCHEDULE

@api_view(["GET"])
@permission_classes([IsAuthenticated])
def schedule(request):
    employee = me(request)

    if employee is None:
        return no_employee()

    week = parse_date(request.query_params.get("week", "")) or timezone.localdate()
    week_start = monday_of(week)

    plan = (
        WeeklyPlan.objects
        .filter(employee=employee, week_start=week_start)
        .prefetch_related("visits__call_point", "visits__report")
        .first()
    )

    if plan is None:
        return Response({"week_start": week_start, "plan": None})

    return Response({
        "week_start": week_start,
        "plan": WeeklyPlanSerializer(plan).data,
    })


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def create_schedule(request):
    """Let an MR fill their own week from their territory.

    generate_plan refuses to overwrite a week the office has already submitted
    or approved, so this cannot be used to wipe an agreed plan.
    """
    employee = me(request)

    if employee is None:
        return no_employee()

    week = parse_date(request.data.get("week_start", "")) or current_week_start()

    plan, created = generate_plan(
        employee=employee,
        week_start=monday_of(week),
        calls_per_day=int(request.data.get("calls_per_day") or 6),
        created_by=request.user,
    )

    # generate_plan hands back the plan untouched rather than raising, so the
    # refusal has to be read off its status.
    if not plan.is_editable:
        return Response(
            {"detail": f"That week is already {plan.status}, so it cannot be "
                       f"regenerated. Ask the office to reopen it.",
             "plan": WeeklyPlanSerializer(plan).data},
            status=status.HTTP_409_CONFLICT,
        )

    if not created and employee.territory_id is None:
        return Response(
            {"detail": "You have no territory set, so there are no call "
                       "points to build a week from."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    return Response(
        {"created": created, "plan": WeeklyPlanSerializer(plan).data},
        status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
    )


# --------------------------------------------------- CALL POINTS & DOCTORS

@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def call_points(request):
    employee = me(request)

    if employee is None:
        return no_employee()

    if request.method == "GET":
        return Response(CallPointSerializer(
            in_territory(employee).select_related("territory")
            .prefetch_related("doctors"),
            many=True,
        ).data)

    if employee.territory_id is None:
        return Response(
            {"detail": "You have no territory set, so a new call point has "
                       "nowhere to go. Ask the office to set yours."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    name = (request.data.get("name") or "").strip()

    if not name:
        return Response(
            {"name": ["A name is required."]},
            status=status.HTTP_400_BAD_REQUEST,
        )

    # An MR adds call points to their own patch and nowhere else, whatever the
    # request body says.
    call_point, created = CallPoint.objects.get_or_create(
        name=name,
        territory_id=employee.territory_id,
        defaults={
            "kind": request.data.get("kind") or "doctor",
            "speciality": request.data.get("speciality") or "",
            "address": request.data.get("address") or "",
            "phone": request.data.get("phone") or "",
        },
    )

    return Response(
        CallPointSerializer(call_point).data,
        status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
    )


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def doctors(request):
    employee = me(request)

    if employee is None:
        return no_employee()

    reachable = in_territory(employee)

    if request.method == "GET":
        return Response(DoctorSerializer(
            Doctor.objects.filter(call_point__in=reachable)
            .select_related("call_point"),
            many=True,
        ).data)

    call_point = reachable.filter(pk=request.data.get("call_point")).first()

    if call_point is None:
        return Response(
            {"call_point": ["Pick a call point in your own territory."]},
            status=status.HTTP_400_BAD_REQUEST,
        )

    name = (request.data.get("name") or "").strip()

    if not name:
        return Response(
            {"name": ["A name is required."]},
            status=status.HTTP_400_BAD_REQUEST,
        )

    # Same doctor at the same place twice is the offline queue retrying, not a
    # second doctor with an identical name.
    doctor, created = Doctor.objects.get_or_create(
        name=name,
        call_point=call_point,
        defaults={
            "speciality": request.data.get("speciality") or "",
            "qualification": request.data.get("qualification") or "",
            "phone": request.data.get("phone") or "",
            "email": request.data.get("email") or "",
            "potential": request.data.get("potential") or "",
            "notes": request.data.get("notes") or "",
            "created_by": request.user,
        },
    )

    return Response(
        DoctorSerializer(doctor).data,
        status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
    )


@api_view(["PATCH"])
@permission_classes([IsAuthenticated])
def update_doctor(request, doctor_id):
    """Correct a doctor's details in place."""
    employee = me(request)

    if employee is None:
        return no_employee()

    doctor = Doctor.objects.filter(
        pk=doctor_id, call_point__in=in_territory(employee)
    ).first()

    if doctor is None:
        return Response(
            {"detail": "No such doctor in your territory."},
            status=status.HTTP_404_NOT_FOUND,
        )

    for field in ("name", "speciality", "qualification", "phone", "email",
                  "potential", "notes"):
        if field in request.data:
            setattr(doctor, field, request.data[field] or "")

    if "is_active" in request.data:
        doctor.is_active = bool(request.data["is_active"])

    doctor.save()

    return Response(DoctorSerializer(doctor).data)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def move_doctor(request, doctor_id):
    """The doctor has left that hospital and now sits somewhere else.

    A move, not a new record: the visit history follows the person.
    """
    employee = me(request)

    if employee is None:
        return no_employee()

    reachable = in_territory(employee)

    doctor = Doctor.objects.filter(
        pk=doctor_id, call_point__in=reachable
    ).first()

    if doctor is None:
        return Response(
            {"detail": "No such doctor in your territory."},
            status=status.HTTP_404_NOT_FOUND,
        )

    destination = reachable.filter(pk=request.data.get("to_call_point")).first()

    if destination is None:
        return Response(
            {"to_call_point": ["Pick a call point in your own territory."]},
            status=status.HTTP_400_BAD_REQUEST,
        )

    move = doctor.move_to(
        destination,
        reason=request.data.get("reason") or "",
        user=request.user,
        on=parse_date(request.data.get("moved_on") or ""),
    )

    return Response({
        "moved": move is not None,
        "doctor": DoctorSerializer(doctor).data,
    })


# ---------------------------------------------------------------- VISITS

@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def visits(request):
    employee = me(request)

    if employee is None:
        return no_employee()

    if request.method == "GET":
        reports = CallReport.objects.filter(employee=employee)

        since = parse_date(request.query_params.get("from", "") or "")

        if since:
            reports = reports.filter(visit_date__gte=since)

        return Response(CallReportSerializer(
            reports.select_related("call_point").prefetch_related("products"),
            many=True,
        ).data)

    return record_visit(request, employee)


@transaction.atomic
def record_visit(request, employee):
    client_uuid = (request.data.get("client_uuid") or "").strip()

    if client_uuid:
        existing = CallReport.objects.filter(
            employee=employee, client_uuid=client_uuid
        ).first()

        if existing is not None:
            # Already had this one. Say so plainly rather than making a second.
            return Response(
                CallReportSerializer(existing).data, status=status.HTTP_200_OK
            )

    reachable = in_territory(employee)
    call_point = reachable.filter(pk=request.data.get("call_point")).first()

    if call_point is None:
        return Response(
            {"call_point": ["Pick a call point in your own territory."]},
            status=status.HTTP_400_BAD_REQUEST,
        )

    doctor = Doctor.objects.filter(
        pk=request.data.get("doctor"), call_point=call_point
    ).first()

    report = CallReport.objects.create(
        employee=employee,
        call_point=call_point,
        doctor=doctor,
        visit_date=parse_date(request.data.get("visit_date") or "")
        or timezone.localdate(),
        visit_time=request.data.get("visit_time") or None,
        doctor_name=(request.data.get("doctor_name")
                     or (doctor.name if doctor else ""))[:200],
        speciality=(request.data.get("speciality")
                    or (doctor.speciality if doctor else ""))[:120],
        outcome=request.data.get("outcome") or CallReport.MET,
        feedback=request.data.get("feedback") or "",
        next_visit_date=parse_date(request.data.get("next_visit_date") or ""),
        client_uuid=client_uuid,
        created_by=request.user,
    )

    product_ids = request.data.get("products") or []

    if product_ids:
        report.products.set(Product.objects.filter(pk__in=product_ids))

    close_scheduled_slot(report, request.data.get("plan_visit"), employee)

    return Response(
        CallReportSerializer(report).data, status=status.HTTP_201_CREATED
    )


def close_scheduled_slot(report, visit_id, employee):
    """Mark the planned call visited or missed, if this was one."""
    if not visit_id:
        return

    # Scoped to this employee's own plans, so a stale id from another phone
    # cannot close somebody else's scheduled call.
    visit = PlanVisit.objects.filter(
        pk=visit_id, plan__employee=employee
    ).first()

    if visit is None:
        return

    report.plan_visit = visit
    report.save(update_fields=["plan_visit"])

    visit.status = "done" if report.outcome == CallReport.MET else "missed"
    visit.remarks = report.feedback[:255]
    visit.save(update_fields=["status", "remarks"])


# ------------------------------------------------------------- PERFORMANCE

@api_view(["GET"])
@permission_classes([IsAuthenticated])
def performance(request):
    """This month's numbers, and the targets they are measured against."""
    employee = me(request)

    if employee is None:
        return no_employee()

    asked = parse_date(f"{request.query_params.get('month', '')}-01")
    start, end = month_range(asked or timezone.localdate())

    reports = CallReport.objects.filter(
        employee=employee, visit_date__gte=start, visit_date__lte=end
    )

    target = Target.objects.filter(employee=employee, month=start).first()

    sales = employee.net_sales(start, end)

    return Response({
        "month": start,
        "actual": {
            "sales_value": sales,
            "commission": employee.commission_on(start, end),
            "commission_percent": employee.commission_percent,
            "call_count": reports.count(),
            "doctor_count": reports.values("call_point_id").distinct().count(),
            "met_count": reports.filter(outcome=CallReport.MET).count(),
        },
        "target": TargetSerializer(target).data if target else None,
    })


# ---------------------------------------------------------------- EXPENSES

@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def expenses(request):
    employee = me(request)

    if employee is None:
        return no_employee()

    if request.method == "GET":
        return Response(ExpenseSerializer(
            Expense.objects.filter(employee=employee).select_related("category"),
            many=True,
        ).data)

    serializer = ExpenseSerializer(data=request.data)

    if not serializer.is_valid():
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    # Claimed for themselves, and always pending: nobody approves their own.
    expense = serializer.save(
        employee=employee,
        territory=employee.territory,
        status=Expense.PENDING,
        submitted_by=request.user,
    )

    return Response(
        ExpenseSerializer(expense).data, status=status.HTTP_201_CREATED
    )
