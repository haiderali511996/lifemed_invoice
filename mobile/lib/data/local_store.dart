import 'dart:convert';

import 'package:path/path.dart' as p;
import 'package:sqflite/sqflite.dart';

/// The phone's own copy of the world.
///
/// Two jobs. It caches what the MR needs to read in a dead zone - the week's
/// schedule, their call points and doctors, the product list. And it holds an
/// outbox of writes that have not reached the server yet, so recording a visit
/// never depends on having a line.
class LocalStore {
  Database? _db;

  Future<Database> get db async => _db ??= await _open();

  Future<Database> _open() async {
    final path = p.join(await getDatabasesPath(), 'lifemed_mr.db');

    return openDatabase(
      path,
      version: 1,
      onCreate: (db, version) async {
        // Cached reads. One row per key, holding raw JSON: the shapes come
        // from the server and mirroring them in columns here would mean a
        // migration every time the API grows a field.
        await db.execute('''
          CREATE TABLE cache (
            key TEXT PRIMARY KEY,
            body TEXT NOT NULL,
            saved_at TEXT NOT NULL
          )
        ''');

        // The outbox. `uuid` is generated on the device and travels with the
        // request, so the server can recognise a resend.
        await db.execute('''
          CREATE TABLE outbox (
            uuid TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            payload TEXT NOT NULL,
            created_at TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT
          )
        ''');

        await db.execute(
          'CREATE INDEX outbox_created ON outbox (created_at)',
        );

        // Records created offline get a negative placeholder id. Once the
        // server has seen one, the real id is kept here so a visit queued
        // against it can still be resolved after the app has been restarted -
        // in memory alone, closing the app between the two would strand the
        // visit for good.
        await db.execute('''
          CREATE TABLE id_map (
            local_id INTEGER PRIMARY KEY,
            server_id INTEGER NOT NULL
          )
        ''');
      },
    );
  }

  // ------------------------------------------------------------- the cache

  Future<void> put(String key, Object body) async {
    final database = await db;

    await database.insert(
      'cache',
      {
        'key': key,
        'body': jsonEncode(body),
        'saved_at': DateTime.now().toIso8601String(),
      },
      conflictAlgorithm: ConflictAlgorithm.replace,
    );
  }

  Future<dynamic> read(String key) async {
    final database = await db;
    final rows = await database.query(
      'cache',
      where: 'key = ?',
      whereArgs: [key],
      limit: 1,
    );

    if (rows.isEmpty) return null;

    return jsonDecode(rows.first['body'] as String);
  }

  Future<DateTime?> savedAt(String key) async {
    final database = await db;
    final rows = await database.query(
      'cache',
      columns: ['saved_at'],
      where: 'key = ?',
      whereArgs: [key],
      limit: 1,
    );

    if (rows.isEmpty) return null;

    return DateTime.tryParse(rows.first['saved_at'] as String);
  }

  // ------------------------------------------------------------ the outbox

  Future<void> queue(String uuid, String kind, Map<String, dynamic> payload) async {
    final database = await db;

    await database.insert(
      'outbox',
      {
        'uuid': uuid,
        'kind': kind,
        'payload': jsonEncode(payload),
        'created_at': DateTime.now().toIso8601String(),
        'attempts': 0,
      },
      // Editing a queued item before it syncs replaces it rather than adding
      // a second copy of the same visit.
      conflictAlgorithm: ConflictAlgorithm.replace,
    );
  }

  Future<List<OutboxItem>> pending({int limit = 50}) async {
    final database = await db;

    final rows = await database.query(
      'outbox',
      orderBy: 'created_at ASC',
      limit: limit,
    );

    return rows.map(OutboxItem.fromRow).toList();
  }

  Future<int> pendingCount() async {
    final database = await db;

    return Sqflite.firstIntValue(
          await database.rawQuery('SELECT COUNT(*) FROM outbox'),
        ) ??
        0;
  }

  Future<void> done(String uuid) async {
    final database = await db;

    await database.delete('outbox', where: 'uuid = ?', whereArgs: [uuid]);
  }

  Future<void> failed(String uuid, String error) async {
    final database = await db;

    await database.rawUpdate(
      'UPDATE outbox SET attempts = attempts + 1, last_error = ? '
      'WHERE uuid = ?',
      [error, uuid],
    );
  }

  // ----------------------------------------------------- offline id mapping

  Future<void> mapId(int localId, int serverId) async {
    final database = await db;

    await database.insert(
      'id_map',
      {'local_id': localId, 'server_id': serverId},
      conflictAlgorithm: ConflictAlgorithm.replace,
    );
  }

  Future<Map<int, int>> idMap() async {
    final database = await db;
    final rows = await database.query('id_map');

    return {
      for (final row in rows)
        row['local_id'] as int: row['server_id'] as int,
    };
  }

  /// The next placeholder id, counting down from -1.
  ///
  /// Negative so it can never collide with a real server id, and stored so two
  /// records created in the same offline stretch do not share one.
  Future<int> nextLocalId() async {
    final database = await db;

    final lowest = Sqflite.firstIntValue(
          await database.rawQuery('SELECT MIN(local_id) FROM id_map'),
        ) ??
        0;

    final queued = await pending(limit: 1000);
    var floor = lowest;

    for (final item in queued) {
      final value = item.payload['local_id'];

      if (value is int && value < floor) floor = value;
    }

    return floor - 1;
  }

  /// Wipe everything on sign-out.
  ///
  /// Anything still queued is discarded with it: a device handed to another
  /// MR must not push the last one's calls under the new token.
  Future<void> clear() async {
    final database = await db;

    await database.delete('cache');
    await database.delete('outbox');
    await database.delete('id_map');
  }
}

class OutboxItem {
  OutboxItem({
    required this.uuid,
    required this.kind,
    required this.payload,
    required this.attempts,
    required this.createdAt,
    this.lastError,
  });

  final String uuid;
  final String kind;
  final Map<String, dynamic> payload;
  final int attempts;
  final DateTime createdAt;
  final String? lastError;

  /// After this many failures the item is shown to the MR rather than retried
  /// silently forever - usually it is a call point that no longer exists.
  bool get isStuck => attempts >= 5;

  factory OutboxItem.fromRow(Map<String, Object?> row) => OutboxItem(
        uuid: row['uuid'] as String,
        kind: row['kind'] as String,
        payload: Map<String, dynamic>.from(
          jsonDecode(row['payload'] as String),
        ),
        attempts: row['attempts'] as int? ?? 0,
        createdAt:
            DateTime.tryParse(row['created_at'] as String? ?? '') ??
                DateTime.now(),
        lastError: row['last_error'] as String?,
      );
}

/// Kinds of queued write. Strings rather than an enum because they are stored
/// in the database and have to survive an app upgrade.
class OutboxKind {
  static const visit = 'visit';
  static const callPoint = 'call_point';
  static const doctor = 'doctor';
  static const doctorUpdate = 'doctor_update';
  static const doctorMove = 'doctor_move';
  static const expense = 'expense';
}
