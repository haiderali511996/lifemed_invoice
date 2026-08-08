import 'dart:async';

import 'package:connectivity_plus/connectivity_plus.dart';

import '../api/client.dart';
import 'local_store.dart';

/// Drains the outbox whenever there is a line.
///
/// Order matters and is the reason this is a queue rather than a set of
/// parallel requests: a visit can refer to a doctor that was also created
/// offline, so the doctor has to land first. Items go up oldest-first and one
/// at a time, and a failure stops the run rather than skipping ahead.
class SyncService {
  SyncService({required this.api, required this.store});

  final ApiClient api;
  final LocalStore store;

  bool _running = false;
  StreamSubscription? _watch;

  final _changes = StreamController<SyncStatus>.broadcast();

  Stream<SyncStatus> get changes => _changes.stream;

  /// Local ids handed out to records created offline, mapped to the real ones
  /// once the server has seen them. A visit queued against "doctor -3" is
  /// rewritten to the real id before it goes up.
  ///
  /// Loaded from the database at the start of every run: an app killed between
  /// pushing a doctor and pushing the visit that names them would otherwise
  /// lose the mapping, and the visit could never be filed.
  Map<int, int> _resolved = {};

  void start() {
    _watch ??= Connectivity().onConnectivityChanged.listen((result) {
      final online = !result.contains(ConnectivityResult.none);

      if (online) unawaited(run());
    });

    unawaited(run());
  }

  void dispose() {
    _watch?.cancel();
    _changes.close();
  }

  Future<SyncStatus> run() async {
    if (_running || !api.isSignedIn) {
      return SyncStatus(pending: await store.pendingCount());
    }

    _running = true;

    _resolved = await store.idMap();

    var pushed = 0;
    String? failure;

    try {
      for (final item in await store.pending()) {
        try {
          await _push(item);
          await store.done(item.uuid);
          pushed++;
        } on OfflineException {
          // The line went again mid-run. Everything still queued stays queued.
          failure = 'No connection.';
          break;
        } on ApiException catch (error) {
          if (error.isAuthFailure) {
            failure = 'Signed out. Sign in again to send your work.';
            break;
          }

          // The server understood and refused: retrying unchanged will not
          // help, so record why and move on to the next item.
          await store.failed(item.uuid, error.message);
        }
      }
    } finally {
      _running = false;
    }

    final status = SyncStatus(
      pending: await store.pendingCount(),
      pushed: pushed,
      error: failure,
    );

    _changes.add(status);

    return status;
  }

  Future<void> _push(OutboxItem item) async {
    final payload = _resolveIds(item.payload);

    switch (item.kind) {
      case OutboxKind.visit:
        await api.recordVisit({...payload, 'client_uuid': item.uuid});
        break;

      case OutboxKind.callPoint:
        final created = await api.createCallPoint(payload);
        await _remember(item.payload['local_id'], created['id']);
        break;

      case OutboxKind.doctor:
        final created = await api.createDoctor(payload);
        await _remember(item.payload['local_id'], created['id']);
        break;

      case OutboxKind.doctorUpdate:
        await api.updateDoctor(payload['id'] as int, payload);
        break;

      case OutboxKind.doctorMove:
        await api.moveDoctor(payload['id'] as int, payload);
        break;

      case OutboxKind.expense:
        await api.createExpense(payload);
        break;

      default:
        // An unknown kind is from a newer version of the app writing into a
        // database an older one is now reading. Drop it rather than loop.
        break;
    }
  }

  Future<void> _remember(dynamic localId, dynamic serverId) async {
    if (localId is! int || serverId is! int) return;

    _resolved[localId] = serverId;

    // Written through immediately rather than at the end of the run: the run
    // is exactly what might not finish.
    await store.mapId(localId, serverId);
  }

  /// Swap any negative placeholder id for the real one the server gave us.
  Map<String, dynamic> _resolveIds(Map<String, dynamic> payload) {
    final copy = Map<String, dynamic>.from(payload)..remove('local_id');

    for (final key in ['call_point', 'doctor', 'to_call_point']) {
      final value = copy[key];

      if (value is int && value < 0) {
        final real = _resolved[value];

        if (real != null) {
          copy[key] = real;
        } else {
          // Whatever it points at has not reached the server yet. It sits
          // earlier in the queue, so this only happens when that item is
          // stuck - leaving the placeholder in means the server refuses this
          // one too, which is right: a visit filed against no doctor is worse
          // than one that waits.
          copy[key] = value;
        }
      }
    }

    return copy;
  }
}

class SyncStatus {
  SyncStatus({this.pending = 0, this.pushed = 0, this.error});

  final int pending;
  final int pushed;
  final String? error;

  bool get isClear => pending == 0;
}
