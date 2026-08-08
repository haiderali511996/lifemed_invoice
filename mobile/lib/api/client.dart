import 'dart:async';
import 'dart:convert';

import 'package:http/http.dart' as http;
import 'package:shared_preferences/shared_preferences.dart';

/// Thrown when the server answered, but said no.
class ApiException implements Exception {
  ApiException(this.statusCode, this.message, [this.fields = const {}]);

  final int statusCode;
  final String message;
  final Map<String, dynamic> fields;

  bool get isAuthFailure => statusCode == 401;

  @override
  String toString() => message;
}

/// Thrown when the server could not be reached at all.
///
/// Kept separate from ApiException on purpose: no signal means "queue it and
/// carry on", while a 400 means "this will never work, tell the MR".
class OfflineException implements Exception {
  OfflineException([this.message = 'No connection.', this.cause]);

  final String message;

  /// The underlying failure, kept so it can be shown when asked for.
  ///
  /// Everything that stops a request reaching the server lands here - no
  /// signal, DNS, a certificate the device will not trust, a refused
  /// connection - and they need different fixes. Collapsing them all into
  /// "No connection" leaves whoever is holding the phone with nowhere to go.
  final Object? cause;

  String get detail => cause == null ? message : '$message\n\n$cause';

  @override
  String toString() => detail;
}

class ApiClient {
  ApiClient({required this.baseUrl, http.Client? httpClient})
      : _http = httpClient ?? http.Client();

  /// e.g. https://invoice.lifemedpharmaceutical.com/api/v1
  final String baseUrl;
  final http.Client _http;

  static const _tokenKey = 'auth_token';
  static const _timeout = Duration(seconds: 20);

  String? _token;

  String? get token => _token;

  bool get isSignedIn => _token != null && _token!.isNotEmpty;

  Future<void> restoreToken() async {
    final prefs = await SharedPreferences.getInstance();
    _token = prefs.getString(_tokenKey);
  }

  Future<void> _storeToken(String? value) async {
    _token = value;

    final prefs = await SharedPreferences.getInstance();

    if (value == null) {
      await prefs.remove(_tokenKey);
    } else {
      await prefs.setString(_tokenKey, value);
    }
  }

  Map<String, String> get _headers => {
        'Content-Type': 'application/json',
        if (isSignedIn) 'Authorization': 'Token $_token',
      };

  Uri _uri(String path, [Map<String, String>? query]) =>
      Uri.parse('$baseUrl$path').replace(
        queryParameters: (query == null || query.isEmpty) ? null : query,
      );

  Future<dynamic> _send(Future<http.Response> Function() request) async {
    final http.Response response;

    try {
      response = await request().timeout(_timeout);
    } on TimeoutException catch (error) {
      throw OfflineException('The server took too long to answer.', error);
    } catch (error) {
      // Socket errors, DNS failures, a rejected certificate. They all mean
      // "queue it and carry on", but the reason is carried along so it can be
      // read off the screen instead of guessed at.
      throw OfflineException('Could not reach the server.', error);
    }

    final dynamic body;

    try {
      body = response.body.isEmpty
          ? <String, dynamic>{}
          : jsonDecode(response.body);
    } on FormatException {
      // An HTML error page from the web server rather than JSON from Django -
      // a 500 before the app is even reached, or a captive portal in the way.
      throw ApiException(
        response.statusCode,
        'The server sent back a page, not data '
        '(HTTP ${response.statusCode}). It may be misconfigured.',
      );
    }

    if (response.statusCode >= 200 && response.statusCode < 300) {
      return body;
    }

    final map = body is Map ? Map<String, dynamic>.from(body) : <String, dynamic>{};

    throw ApiException(
      response.statusCode,
      _readMessage(map, response.statusCode),
      map,
    );
  }

  /// Django sends `{"detail": ...}` for whole-request errors and
  /// `{"field": ["..."]}` for form errors. Surface whichever is there, so the
  /// MR sees "Pick a call point in your own territory" and not "Error 400".
  String _readMessage(Map<String, dynamic> body, int statusCode) {
    if (body['detail'] != null) return body['detail'].toString();

    for (final value in body.values) {
      if (value is List && value.isNotEmpty) return value.first.toString();
      if (value is String && value.isNotEmpty) return value;
    }

    return 'Something went wrong (error $statusCode).';
  }

  Future<dynamic> get(String path, [Map<String, String>? query]) =>
      _send(() => _http.get(_uri(path, query), headers: _headers));

  Future<dynamic> post(String path, Map<String, dynamic> body) => _send(
        () => _http.post(_uri(path),
            headers: _headers, body: jsonEncode(body)),
      );

  Future<dynamic> patch(String path, Map<String, dynamic> body) => _send(
        () => _http.patch(_uri(path),
            headers: _headers, body: jsonEncode(body)),
      );

  // ------------------------------------------------------------- endpoints

  Future<Map<String, dynamic>> login(String username, String password) async {
    // Sent without a token: a stale one from a previous user would be rejected
    // before the credentials were even looked at.
    await _storeToken(null);

    final body = Map<String, dynamic>.from(await post(
      '/auth/login/',
      {'username': username, 'password': password},
    ));

    await _storeToken(body['token'] as String?);

    return body;
  }

  Future<void> logout() async {
    try {
      await post('/auth/logout/', {});
    } on OfflineException {
      // Signing out has to work on a train. The token dies on the device now
      // and the server's copy is replaced at the next login anyway.
    } finally {
      await _storeToken(null);
    }
  }

  Future<Map<String, dynamic>> bootstrap() async =>
      Map<String, dynamic>.from(await get('/bootstrap/'));

  Future<Map<String, dynamic>> schedule(DateTime week) async =>
      Map<String, dynamic>.from(await get(
        '/schedule/',
        {'week': week.toIso8601String().split('T').first},
      ));

  Future<Map<String, dynamic>> generateSchedule(
    DateTime week, {
    int callsPerDay = 6,
  }) async =>
      Map<String, dynamic>.from(await post('/schedule/generate/', {
        'week_start': week.toIso8601String().split('T').first,
        'calls_per_day': callsPerDay,
      }));

  Future<Map<String, dynamic>> createCallPoint(Map<String, dynamic> data) async =>
      Map<String, dynamic>.from(await post('/call-points/', data));

  Future<Map<String, dynamic>> createDoctor(Map<String, dynamic> data) async =>
      Map<String, dynamic>.from(await post('/doctors/', data));

  Future<Map<String, dynamic>> updateDoctor(
          int id, Map<String, dynamic> data) async =>
      Map<String, dynamic>.from(await patch('/doctors/$id/', data));

  Future<Map<String, dynamic>> moveDoctor(
          int id, Map<String, dynamic> data) async =>
      Map<String, dynamic>.from(await post('/doctors/$id/move/', data));

  Future<Map<String, dynamic>> recordVisit(Map<String, dynamic> data) async =>
      Map<String, dynamic>.from(await post('/visits/', data));

  Future<Map<String, dynamic>> performance([DateTime? month]) async =>
      Map<String, dynamic>.from(await get(
        '/performance/',
        month == null
            ? null
            : {'month': '${month.year}-${month.month.toString().padLeft(2, '0')}'},
      ));

  Future<Map<String, dynamic>> createExpense(Map<String, dynamic> data) async =>
      Map<String, dynamic>.from(await post('/expenses/', data));

  Future<List<dynamic>> expenses() async =>
      List<dynamic>.from(await get('/expenses/'));
}
