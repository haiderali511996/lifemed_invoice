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

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();

  final state = AppState(
    api: ApiClient(baseUrl: apiBase),
    store: LocalStore(),
  );

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
