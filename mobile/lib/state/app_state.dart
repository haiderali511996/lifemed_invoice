import 'dart:async';

import 'package:flutter/foundation.dart';
import 'package:uuid/uuid.dart';

import '../api/client.dart';
import '../data/local_store.dart';
import '../data/sync.dart';
import '../models/models.dart';

/// Everything the screens read and write.
///
/// The rule throughout: a write goes into the outbox and the local cache
/// first, then the UI updates, then sync pushes it when it can. Nothing an MR
/// does is allowed to wait on the network.
class AppState extends ChangeNotifier {
  AppState({required this.api, required this.store})
      : sync = SyncService(api: api, store: store);

  final ApiClient api;
  final LocalStore store;
  final SyncService sync;

  static const _uuid = Uuid();

  Employee? employee;
  List<CallPoint> callPoints = [];
  List<Product> products = [];
  List<ExpenseCategory> expenseCategories = [];
  WeeklyPlan? plan;
  Performance? performance;

  int pending = 0;
  bool loading = false;
  DateTime? lastSynced;
  String? banner;

  bool get isSignedIn => api.isSignedIn && employee != null;

  List<Doctor> get allDoctors =>
      [for (final point in callPoints) ...point.doctors];

  // ------------------------------------------------------------- lifecycle

  Future<void> boot() async {
    await api.restoreToken();

    if (!api.isSignedIn) return;

    await _loadFromCache();

    sync.changes.listen((status) {
      pending = status.pending;
      if (status.error != null) banner = status.error;
      notifyListeners();
    });

    sync.start();

    // Best effort: if there is no line, the cache we just loaded stands.
    unawaited(refresh());
  }

  Future<bool> signIn(String username, String password) async {
    loading = true;
    banner = null;
    notifyListeners();

    try {
      final body = await api.login(username, password);
      employee = Employee.fromJson(
        Map<String, dynamic>.from(body['employee']),
      );

      await store.clear();
      await refresh();

      sync.start();

      return true;
    } on ApiException catch (error) {
      banner = error.message;

      return false;
    } on OfflineException catch (error) {
      // The underlying reason, not just "offline": on a phone with full signal
      // this is the difference between a wrong address, a rejected
      // certificate, and a server that is actually down.
      banner = 'Could not reach the server.\n\n${error.cause ?? ''}\n\n'
          'Signing in for the first time needs a connection.';

      return false;
    } finally {
      loading = false;
      notifyListeners();
    }
  }

  Future<void> signOut() async {
    await api.logout();
    await store.clear();

    employee = null;
    callPoints = [];
    products = [];
    plan = null;
    performance = null;
    pending = 0;

    notifyListeners();
  }

  // ------------------------------------------------------------ loading

  Future<void> _loadFromCache() async {
    final cached = await store.read('bootstrap');

    if (cached is Map) {
      _applyBootstrap(Map<String, dynamic>.from(cached));
    }

    final cachedPlan = await store.read('plan');

    if (cachedPlan is Map) {
      plan = WeeklyPlan.fromJson(Map<String, dynamic>.from(cachedPlan));
    }

    lastSynced = await store.savedAt('bootstrap');
    pending = await store.pendingCount();

    notifyListeners();
  }

  void _applyBootstrap(Map<String, dynamic> body) {
    if (body['employee'] != null) {
      employee = Employee.fromJson(
        Map<String, dynamic>.from(body['employee']),
      );
    }

    callPoints = (body['call_points'] as List? ?? [])
        .map((c) => CallPoint.fromJson(Map<String, dynamic>.from(c)))
        .toList();

    products = (body['products'] as List? ?? [])
        .map((p) => Product.fromJson(Map<String, dynamic>.from(p)))
        .toList();

    expenseCategories = (body['expense_categories'] as List? ?? [])
        .map((c) => ExpenseCategory.fromJson(Map<String, dynamic>.from(c)))
        .toList();
  }

  /// Pull fresh data, then push anything queued.
  ///
  /// Refresh first so a stale cache is replaced even when the outbox has an
  /// item the server keeps refusing.
  Future<void> refresh() async {
    loading = true;
    notifyListeners();

    try {
      final body = await api.bootstrap();

      _applyBootstrap(body);
      await store.put('bootstrap', body);
      lastSynced = DateTime.now();

      await loadSchedule(mondayOf(DateTime.now()));
      await loadPerformance();

      banner = null;
    } on OfflineException {
      // Expected in the field, and not worth shouting about: the header shows
      // when the cache was last filled.
    } on ApiException catch (error) {
      if (error.isAuthFailure) {
        await signOut();
        banner = 'Your session expired. Sign in again.';
      } else {
        banner = error.message;
      }
    } finally {
      loading = false;
      notifyListeners();
    }

    final status = await sync.run();
    pending = status.pending;
    notifyListeners();
  }

  Future<void> loadSchedule(DateTime week) async {
    try {
      final body = await api.schedule(week);

      if (body['plan'] != null) {
        plan = WeeklyPlan.fromJson(Map<String, dynamic>.from(body['plan']));
        await store.put('plan', body['plan']);
      } else {
        plan = null;
      }
    } on OfflineException {
      // Keep whatever is cached.
    } finally {
      notifyListeners();
    }
  }

  Future<String?> generateSchedule(DateTime week, {int callsPerDay = 6}) async {
    try {
      final body = await api.generateSchedule(week, callsPerDay: callsPerDay);

      plan = WeeklyPlan.fromJson(Map<String, dynamic>.from(body['plan']));
      await store.put('plan', body['plan']);
      notifyListeners();

      return null;
    } on OfflineException {
      // Building a week needs the whole territory's visit history, which is
      // not on the phone, so this is one of the few things that needs a line.
      return 'Building a schedule needs a connection.';
    } on ApiException catch (error) {
      return error.message;
    }
  }

  Future<void> loadPerformance([DateTime? month]) async {
    try {
      performance = Performance.fromJson(await api.performance(month));
    } on OfflineException {
      // Numbers go stale rather than blank.
    } finally {
      notifyListeners();
    }
  }

  // -------------------------------------------------------------- writes

  /// Record a visit. Always succeeds: it goes into the outbox first.
  Future<void> recordVisit({
    required int callPointId,
    int? doctorId,
    int? planVisitId,
    required DateTime visitDate,
    required String outcome,
    String doctorName = '',
    String speciality = '',
    String feedback = '',
    DateTime? nextVisit,
    List<int> productIds = const [],
  }) async {
    final uuid = _uuid.v4();

    await store.queue(uuid, OutboxKind.visit, {
      'call_point': callPointId,
      if (doctorId != null) 'doctor': doctorId,
      if (planVisitId != null) 'plan_visit': planVisitId,
      'visit_date': _day(visitDate),
      'outcome': outcome,
      'doctor_name': doctorName,
      'speciality': speciality,
      'feedback': feedback,
      if (nextVisit != null) 'next_visit_date': _day(nextVisit),
      'products': productIds,
    });

    _markVisited(planVisitId, outcome);

    pending = await store.pendingCount();
    notifyListeners();

    unawaited(sync.run().then((status) {
      pending = status.pending;
      notifyListeners();
    }));
  }

  /// Show the scheduled call as done straight away, without waiting for sync.
  void _markVisited(int? planVisitId, String outcome) {
    if (planVisitId == null || plan == null) return;

    final updated = [
      for (final visit in plan!.visits)
        if (visit.id == planVisitId)
          PlanVisit(
            id: visit.id,
            day: visit.day,
            visitDate: visit.visitDate,
            callPointId: visit.callPointId,
            callPointName: visit.callPointName,
            address: visit.address,
            objective: visit.objective,
            status: outcome == 'met' ? 'done' : 'missed',
            reported: true,
          )
        else
          visit,
    ];

    plan = WeeklyPlan(
      id: plan!.id,
      weekStart: plan!.weekStart,
      status: plan!.status,
      visits: updated,
    );
  }

  Future<CallPoint> addCallPoint({
    required String name,
    String kind = 'doctor',
    String address = '',
    String phone = '',
  }) async {
    final localId = await store.nextLocalId();

    await store.queue(_uuid.v4(), OutboxKind.callPoint, {
      'local_id': localId,
      'name': name,
      'kind': kind,
      'address': address,
      'phone': phone,
    });

    final created = CallPoint(
      id: localId,
      name: name,
      kind: kind,
      address: address,
      phone: phone,
      doctors: const [],
    );

    callPoints = [...callPoints, created];
    await _cacheCallPoints();

    pending = await store.pendingCount();
    notifyListeners();

    unawaited(sync.run());

    return created;
  }

  Future<Doctor> addDoctor({
    required int callPointId,
    required String name,
    String speciality = '',
    String qualification = '',
    String phone = '',
    String potential = '',
  }) async {
    final localId = await store.nextLocalId();

    await store.queue(_uuid.v4(), OutboxKind.doctor, {
      'local_id': localId,
      'call_point': callPointId,
      'name': name,
      'speciality': speciality,
      'qualification': qualification,
      'phone': phone,
      'potential': potential,
    });

    final point = callPoints.firstWhere((c) => c.id == callPointId);

    final created = Doctor(
      id: localId,
      name: name,
      speciality: speciality,
      qualification: qualification,
      callPointId: callPointId,
      callPointName: point.name,
      phone: phone,
      potential: potential,
    );

    _replaceCallPoint(point, [...point.doctors, created]);

    pending = await store.pendingCount();
    notifyListeners();

    unawaited(sync.run());

    return created;
  }

  Future<void> updateDoctor(Doctor doctor, Map<String, dynamic> changes) async {
    await store.queue(_uuid.v4(), OutboxKind.doctorUpdate, {
      'id': doctor.id,
      ...changes,
    });

    final point = callPoints.firstWhere((c) => c.id == doctor.callPointId);

    _replaceCallPoint(point, [
      for (final existing in point.doctors)
        if (existing.id == doctor.id)
          Doctor.fromJson({...doctor.toJson(), ...changes})
        else
          existing,
    ]);

    pending = await store.pendingCount();
    notifyListeners();

    unawaited(sync.run());
  }

  /// The doctor has left one place for another.
  Future<void> moveDoctor(Doctor doctor, int toCallPointId,
      {String reason = ''}) async {
    await store.queue(_uuid.v4(), OutboxKind.doctorMove, {
      'id': doctor.id,
      'to_call_point': toCallPointId,
      'reason': reason,
      'moved_on': _day(DateTime.now()),
    });

    final from = callPoints.firstWhere((c) => c.id == doctor.callPointId);
    final to = callPoints.firstWhere((c) => c.id == toCallPointId);

    final moved = Doctor.fromJson({
      ...doctor.toJson(),
      'call_point': toCallPointId,
      'call_point_name': to.name,
    });

    _replaceCallPoint(
      from,
      from.doctors.where((d) => d.id != doctor.id).toList(),
    );

    final destination = callPoints.firstWhere((c) => c.id == toCallPointId);
    _replaceCallPoint(destination, [...destination.doctors, moved]);

    pending = await store.pendingCount();
    notifyListeners();

    unawaited(sync.run());
  }

  /// Send an order to the office.
  ///
  /// Queued like everything else, so an MR taking an order in a pharmacy with
  /// no signal still gets it away. It reserves no stock and fixes no price -
  /// the office decides both when they raise the invoice.
  Future<void> placeOrder({
    required String customerName,
    int? customerId,
    int? callPointId,
    required List<OrderLine> lines,
    String deliveryAddress = '',
    String contactNumber = '',
    String note = '',
    DateTime? requiredBy,
  }) async {
    await store.queue(_uuid.v4(), OutboxKind.order, {
      if (customerId != null) 'customer': customerId,
      'customer_name': customerName,
      if (callPointId != null) 'call_point': callPointId,
      'delivery_address': deliveryAddress,
      'contact_number': contactNumber,
      'note': note,
      if (requiredBy != null) 'required_by': _day(requiredBy),
      'items': lines.map((line) => line.toJson()).toList(),
    });

    pending = await store.pendingCount();
    notifyListeners();

    unawaited(sync.run().then((status) {
      pending = status.pending;
      notifyListeners();
    }));
  }

  /// Orders the office has seen, newest first, with anything still queued on
  /// the phone shown above them so an MR can tell the difference.
  Future<List<Order>> loadOrders() async {
    final queued = await store.pending(limit: 200);

    final unsent = [
      for (final item in queued)
        if (item.kind == OutboxKind.order)
          Order(
            id: -item.createdAt.millisecondsSinceEpoch,
            orderNo: 'not sent yet',
            customerName: '${item.payload['customer_name'] ?? ''}',
            status: 'queued',
            statusLabel: item.isStuck
                ? 'Could not send — ${item.lastError ?? 'unknown error'}'
                : 'Waiting for signal',
            total: 0,
            lines: const [],
            placedAt: item.createdAt,
            pending: true,
          ),
    ];

    try {
      final sent = (await api.orders())
          .map((row) => Order.fromJson(Map<String, dynamic>.from(row)))
          .toList();

      await store.put('orders', await api.orders());

      return [...unsent, ...sent];
    } on OfflineException {
      final cached = await store.read('orders');

      if (cached is List) {
        return [
          ...unsent,
          ...cached.map(
            (row) => Order.fromJson(Map<String, dynamic>.from(row)),
          ),
        ];
      }

      return unsent;
    }
  }

  Future<String?> cancelOrder(Order order) async {
    try {
      await api.cancelOrder(order.id);

      return null;
    } on OfflineException {
      return 'Cancelling needs a connection.';
    } on ApiException catch (error) {
      return error.message;
    }
  }

  Future<void> claimExpense({
    required int categoryId,
    required double amount,
    required DateTime date,
    String description = '',
  }) async {
    await store.queue(_uuid.v4(), OutboxKind.expense, {
      'category': categoryId,
      'amount': amount.toStringAsFixed(2),
      'date': _day(date),
      'description': description,
    });

    pending = await store.pendingCount();
    notifyListeners();

    unawaited(sync.run());
  }

  // ------------------------------------------------------------- helpers

  void _replaceCallPoint(CallPoint point, List<Doctor> doctors) {
    callPoints = [
      for (final existing in callPoints)
        if (existing.id == point.id)
          CallPoint(
            id: existing.id,
            name: existing.name,
            kind: existing.kind,
            address: existing.address,
            phone: existing.phone,
            doctors: doctors,
          )
        else
          existing,
    ];

    unawaited(_cacheCallPoints());
  }

  Future<void> _cacheCallPoints() async {
    final cached = await store.read('bootstrap');

    if (cached is Map) {
      final body = Map<String, dynamic>.from(cached);
      body['call_points'] = callPoints.map((c) => c.toJson()).toList();
      await store.put('bootstrap', body);
    }
  }

  static String _day(DateTime value) =>
      value.toIso8601String().split('T').first;
}

/// Monday of the week a date falls in. Plans run Monday to Saturday.
DateTime mondayOf(DateTime value) {
  final date = DateTime(value.year, value.month, value.day);

  return date.subtract(Duration(days: date.weekday - DateTime.monday));
}
