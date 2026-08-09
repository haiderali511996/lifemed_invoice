/// Plain data classes mirroring the API payloads.
///
/// Everything parses defensively: a field the server stops sending, or an
/// older app talking to a newer server, should degrade rather than crash on a
/// null. An MR halfway through a round cannot debug a type error.

int _asInt(dynamic value) =>
    value is int ? value : int.tryParse('${value ?? ''}') ?? 0;

double _asDouble(dynamic value) =>
    value is num ? value.toDouble() : double.tryParse('${value ?? ''}') ?? 0;

String _asString(dynamic value) => value?.toString() ?? '';

class Employee {
  Employee({
    required this.id,
    required this.code,
    required this.fullName,
    required this.designation,
    required this.territoryName,
    required this.commissionPercent,
  });

  final int id;
  final String code;
  final String fullName;
  final String designation;
  final String territoryName;
  final double commissionPercent;

  factory Employee.fromJson(Map<String, dynamic> json) => Employee(
        id: _asInt(json['id']),
        code: _asString(json['employee_code']),
        fullName: _asString(json['full_name']),
        designation: _asString(json['designation']),
        territoryName: _asString(json['territory_name']),
        commissionPercent: _asDouble(json['commission_percent']),
      );

  Map<String, dynamic> toJson() => {
        'id': id,
        'employee_code': code,
        'full_name': fullName,
        'designation': designation,
        'territory_name': territoryName,
        'commission_percent': commissionPercent,
      };
}

class Doctor {
  Doctor({
    required this.id,
    required this.name,
    required this.speciality,
    required this.qualification,
    required this.callPointId,
    required this.callPointName,
    required this.phone,
    required this.potential,
  });

  final int id;
  final String name;
  final String speciality;
  final String qualification;
  final int callPointId;
  final String callPointName;
  final String phone;
  final String potential;

  factory Doctor.fromJson(Map<String, dynamic> json) => Doctor(
        id: _asInt(json['id']),
        name: _asString(json['name']),
        speciality: _asString(json['speciality']),
        qualification: _asString(json['qualification']),
        callPointId: _asInt(json['call_point']),
        callPointName: _asString(json['call_point_name']),
        phone: _asString(json['phone']),
        potential: _asString(json['potential']),
      );

  Map<String, dynamic> toJson() => {
        'id': id,
        'name': name,
        'speciality': speciality,
        'qualification': qualification,
        'call_point': callPointId,
        'call_point_name': callPointName,
        'phone': phone,
        'potential': potential,
      };
}

class CallPoint {
  CallPoint({
    required this.id,
    required this.name,
    required this.kind,
    required this.address,
    required this.phone,
    required this.doctors,
  });

  final int id;
  final String name;
  final String kind;
  final String address;
  final String phone;
  final List<Doctor> doctors;

  factory CallPoint.fromJson(Map<String, dynamic> json) => CallPoint(
        id: _asInt(json['id']),
        name: _asString(json['name']),
        kind: _asString(json['kind']),
        address: _asString(json['address']),
        phone: _asString(json['phone']),
        doctors: (json['doctors'] as List? ?? [])
            .map((d) => Doctor.fromJson(Map<String, dynamic>.from(d)))
            .toList(),
      );

  Map<String, dynamic> toJson() => {
        'id': id,
        'name': name,
        'kind': kind,
        'address': address,
        'phone': phone,
        'doctors': doctors.map((d) => d.toJson()).toList(),
      };
}

class Product {
  Product({required this.id, required this.name, required this.code});

  final int id;
  final String name;
  final String code;

  factory Product.fromJson(Map<String, dynamic> json) => Product(
        id: _asInt(json['id']),
        name: _asString(json['name']),
        code: _asString(json['code']),
      );

  Map<String, dynamic> toJson() => {'id': id, 'name': name, 'code': code};
}

class ExpenseCategory {
  ExpenseCategory({required this.id, required this.name});

  final int id;
  final String name;

  factory ExpenseCategory.fromJson(Map<String, dynamic> json) =>
      ExpenseCategory(
        id: _asInt(json['id']),
        name: _asString(json['name']),
      );

  Map<String, dynamic> toJson() => {'id': id, 'name': name};
}

class PlanVisit {
  PlanVisit({
    required this.id,
    required this.day,
    required this.visitDate,
    required this.callPointId,
    required this.callPointName,
    required this.address,
    required this.objective,
    required this.status,
    required this.reported,
  });

  final int id;
  final int day;
  final DateTime visitDate;
  final int callPointId;
  final String callPointName;
  final String address;
  final String objective;
  final String status;
  final bool reported;

  bool get isDone => status == 'done' || reported;

  factory PlanVisit.fromJson(Map<String, dynamic> json) => PlanVisit(
        id: _asInt(json['id']),
        day: _asInt(json['day']),
        visitDate:
            DateTime.tryParse(_asString(json['visit_date'])) ?? DateTime.now(),
        callPointId: _asInt(json['call_point']),
        callPointName: _asString(json['call_point_name']),
        address: _asString(json['address']),
        objective: _asString(json['objective']),
        status: _asString(json['status']),
        reported: json['reported'] != null,
      );

  Map<String, dynamic> toJson() => {
        'id': id,
        'day': day,
        'visit_date': visitDate.toIso8601String().split('T').first,
        'call_point': callPointId,
        'call_point_name': callPointName,
        'address': address,
        'objective': objective,
        'status': status,
        'reported': reported ? 1 : null,
      };
}

class WeeklyPlan {
  WeeklyPlan({
    required this.id,
    required this.weekStart,
    required this.status,
    required this.visits,
  });

  final int id;
  final DateTime weekStart;
  final String status;
  final List<PlanVisit> visits;

  List<PlanVisit> onDay(int day) =>
      visits.where((v) => v.day == day).toList();

  factory WeeklyPlan.fromJson(Map<String, dynamic> json) => WeeklyPlan(
        id: _asInt(json['id']),
        weekStart:
            DateTime.tryParse(_asString(json['week_start'])) ?? DateTime.now(),
        status: _asString(json['status']),
        visits: (json['visits'] as List? ?? [])
            .map((v) => PlanVisit.fromJson(Map<String, dynamic>.from(v)))
            .toList(),
      );

  Map<String, dynamic> toJson() => {
        'id': id,
        'week_start': weekStart.toIso8601String().split('T').first,
        'status': status,
        'visits': visits.map((v) => v.toJson()).toList(),
      };
}

class OrderLine {
  OrderLine({
    required this.productId,
    required this.productName,
    required this.qty,
    required this.unitPrice,
    required this.discount,
  });

  final int productId;
  final String productName;
  final int qty;
  final double unitPrice;
  final double discount;

  double get lineTotal {
    final gross = unitPrice * qty;

    return gross - (gross * discount / 100);
  }

  factory OrderLine.fromJson(Map<String, dynamic> json) => OrderLine(
        productId: _asInt(json['product']),
        productName: _asString(json['product_name']),
        qty: _asInt(json['qty']),
        unitPrice: _asDouble(json['unit_price']),
        discount: _asDouble(json['discount']),
      );

  Map<String, dynamic> toJson() => {
        'product': productId,
        'qty': qty,
        'unit_price': unitPrice.toStringAsFixed(2),
        'discount': discount.toStringAsFixed(2),
      };
}

class Order {
  Order({
    required this.id,
    required this.orderNo,
    required this.customerName,
    required this.status,
    required this.statusLabel,
    required this.total,
    required this.lines,
    required this.placedAt,
    this.invoiceNo,
    this.pending = false,
  });

  final int id;
  final String orderNo;
  final String customerName;
  final String status;
  final String statusLabel;
  final double total;
  final List<OrderLine> lines;
  final DateTime placedAt;
  final String? invoiceNo;

  /// Still sitting in the outbox, never yet seen by the office.
  final bool pending;

  bool get canCancel =>
      !pending && (status == 'pending' || status == 'approved');

  factory Order.fromJson(Map<String, dynamic> json) => Order(
        id: _asInt(json['id']),
        orderNo: _asString(json['order_no']),
        customerName: _asString(json['customer_name']),
        status: _asString(json['status']),
        statusLabel: _asString(json['status_label']),
        total: _asDouble(json['total']),
        lines: (json['items'] as List? ?? [])
            .map((line) => OrderLine.fromJson(Map<String, dynamic>.from(line)))
            .toList(),
        placedAt:
            DateTime.tryParse(_asString(json['created_at'])) ?? DateTime.now(),
        invoiceNo: json['invoice_no']?.toString(),
      );
}

/// One line of "target vs actual".
class Measure {
  Measure({
    required this.label,
    required this.target,
    required this.actual,
    required this.percent,
    this.isMoney = false,
  });

  final String label;
  final double target;
  final double actual;
  final int percent;
  final bool isMoney;

  factory Measure.fromJson(String label, Map<String, dynamic> json,
          {bool isMoney = false}) =>
      Measure(
        label: label,
        target: _asDouble(json['target']),
        actual: _asDouble(json['actual']),
        percent: _asInt(json['percent']),
        isMoney: isMoney,
      );
}

class Performance {
  Performance({
    required this.month,
    required this.sales,
    required this.commission,
    required this.commissionPercent,
    required this.callCount,
    required this.doctorCount,
    required this.metCount,
    required this.measures,
    required this.hasTarget,
  });

  final DateTime month;
  final double sales;
  final double commission;
  final double commissionPercent;
  final int callCount;
  final int doctorCount;
  final int metCount;
  final List<Measure> measures;
  final bool hasTarget;

  factory Performance.fromJson(Map<String, dynamic> json) {
    final actual = Map<String, dynamic>.from(json['actual'] ?? {});
    final target = json['target'];
    final measures = <Measure>[];

    if (target != null) {
      final achievement =
          Map<String, dynamic>.from(target['achievement'] ?? {});

      void add(String key, String label, {bool isMoney = false}) {
        if (achievement[key] != null) {
          measures.add(Measure.fromJson(
              label, Map<String, dynamic>.from(achievement[key]),
              isMoney: isMoney));
        }
      }

      add('sales_value', 'Sales', isMoney: true);
      add('call_count', 'Calls');
      add('doctor_count', 'Doctors seen');

      for (final line in (achievement['products'] as List? ?? [])) {
        final row = Map<String, dynamic>.from(line);
        measures.add(Measure.fromJson(_asString(row['product']), row));
      }
    }

    return Performance(
      month: DateTime.tryParse(_asString(json['month'])) ?? DateTime.now(),
      sales: _asDouble(actual['sales_value']),
      commission: _asDouble(actual['commission']),
      commissionPercent: _asDouble(actual['commission_percent']),
      callCount: _asInt(actual['call_count']),
      doctorCount: _asInt(actual['doctor_count']),
      metCount: _asInt(actual['met_count']),
      measures: measures,
      hasTarget: target != null,
    );
  }
}
