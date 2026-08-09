import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import 'api/client.dart';
import 'data/local_store.dart';
import 'screens/home.dart';
import 'screens/login.dart';
import 'state/app_state.dart';
import 'theme.dart';

/// Where the ERP lives. Overridable at build time so a test build can point at
/// a staging server without touching the source:
///   flutter build apk --dart-define=API_BASE=https://staging.example.com/api/v1
const apiBase = String.fromEnvironment(
  'API_BASE',
  defaultValue: 'https://invoice.lifemedpharmaceutical.com/api/v1',
);

/// Shown on the login screen so it is obvious which server a build points at -
/// a test APK aimed at localhost looks identical to a live one otherwise.
String get apiHost => Uri.parse(apiBase).host;

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();

  final api = ApiClient(baseUrl: apiBase);

  // Built before anything is fetched, so the very first request already
  // trusts our server's CA on devices whose own trust store does not.
  api.httpClient = await buildHttpClient();

  final state = AppState(api: api, store: LocalStore());

  await state.boot();

  runApp(
    ChangeNotifierProvider.value(value: state, child: const LifeMedApp()),
  );
}

class LifeMedApp extends StatelessWidget {
  const LifeMedApp({super.key});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'LifeMed Pharma',
      debugShowCheckedModeBanner: false,
      theme: buildTheme(),
      home: Consumer<AppState>(
        builder: (context, state, _) =>
            state.isSignedIn ? const HomeScreen() : const LoginScreen(),
      ),
    );
  }
}
