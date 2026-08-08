"""What the mobile app sees.

Deliberately flat and small: a phone on a patchy line pays for every byte, and
the app caches these payloads to work offline, so anything included here has to
be worth storing on the device too.
"""

from rest_framework import serializers

from ..models import (
    CallPoint,
    CallReport,
    Doctor,
    DoctorMove,
    Employee,
    Expense,
    ExpenseCategory,
    PlanVisit,
    Product,
    Target,
    Territory,
    WeeklyPlan,
)


class TerritorySerializer(serializers.ModelSerializer):
    class Meta:
        model = Territory
        fields = ["id", "name", "code", "city", "region"]


class DoctorSerializer(serializers.ModelSerializer):
    call_point_name = serializers.CharField(
        source="call_point.name", read_only=True
    )
    territory_id = serializers.IntegerField(
        source="call_point.territory_id", read_only=True
    )

    class Meta:
        model = Doctor
        fields = [
            "id", "name", "speciality", "qualification", "call_point",
            "call_point_name", "territory_id", "phone", "email", "potential",
            "notes", "is_active", "updated_at",
        ]
        read_only_fields = ["updated_at"]


class CallPointSerializer(serializers.ModelSerializer):
    doctors = DoctorSerializer(many=True, read_only=True)
    territory_name = serializers.CharField(
        source="territory.name", read_only=True
    )

    class Meta:
        model = CallPoint
        fields = [
            "id", "name", "kind", "speciality", "territory", "territory_name",
            "address", "phone", "estimated_volume", "is_active", "doctors",
        ]


class ProductSerializer(serializers.ModelSerializer):
    class Meta:
        model = Product
        fields = ["id", "code", "name", "pack_size", "is_active"]


class PlanVisitSerializer(serializers.ModelSerializer):
    call_point_name = serializers.CharField(
        source="call_point.name", read_only=True
    )
    address = serializers.CharField(source="call_point.address", read_only=True)
    visit_date = serializers.DateField(read_only=True)
    reported = serializers.SerializerMethodField()

    class Meta:
        model = PlanVisit
        fields = [
            "id", "day", "visit_date", "call_point", "call_point_name",
            "address", "objective", "status", "remarks", "reported",
        ]

    def get_reported(self, visit):
        report = getattr(visit, "report", None)

        return report.pk if report else None


class WeeklyPlanSerializer(serializers.ModelSerializer):
    visits = PlanVisitSerializer(many=True, read_only=True)
    employee_name = serializers.CharField(
        source="employee.full_name", read_only=True
    )

    class Meta:
        model = WeeklyPlan
        fields = [
            "id", "employee", "employee_name", "week_start", "week_end",
            "status", "visits",
        ]


class CallReportSerializer(serializers.ModelSerializer):
    """A visit, as recorded on the phone.

    `employee` is never accepted from the device: it comes from the token, so
    a report can only ever be filed by whoever is signed in.
    """

    call_point_name = serializers.CharField(
        source="call_point.name", read_only=True
    )
    samples_given = serializers.IntegerField(read_only=True)

    class Meta:
        model = CallReport
        fields = [
            "id", "client_uuid", "call_point", "call_point_name", "doctor",
            "plan_visit", "visit_date", "visit_time", "doctor_name",
            "speciality", "outcome", "products", "feedback", "next_visit_date",
            "samples_given", "created_at",
        ]
        read_only_fields = ["created_at", "samples_given"]


class ExpenseCategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = ExpenseCategory
        fields = ["id", "name", "code", "per_employee"]


class ExpenseSerializer(serializers.ModelSerializer):
    category_name = serializers.CharField(source="category.name", read_only=True)

    class Meta:
        model = Expense
        fields = [
            "id", "category", "category_name", "amount", "date",
            "description", "reference", "status",
        ]
        read_only_fields = ["status"]


class DoctorMoveSerializer(serializers.ModelSerializer):
    class Meta:
        model = DoctorMove
        fields = [
            "id", "doctor", "from_call_point", "to_call_point",
            "moved_on", "reason",
        ]
        read_only_fields = ["from_call_point"]


class EmployeeSerializer(serializers.ModelSerializer):
    territory_name = serializers.CharField(
        source="territory.name", read_only=True
    )

    class Meta:
        model = Employee
        fields = [
            "id", "employee_code", "full_name", "designation", "phone",
            "email", "territory", "territory_name", "commission_percent",
        ]


class TargetSerializer(serializers.ModelSerializer):
    achievement = serializers.SerializerMethodField()

    class Meta:
        model = Target
        fields = [
            "id", "month", "sales_value", "call_count", "doctor_count",
            "note", "achievement",
        ]

    def get_achievement(self, target):
        return target.achievement()
