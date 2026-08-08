# LifeMed Pharma — MR App

The field app: an MR's day, their doctors, their targets and their calls.

**This has not been compiled.** There is no Flutter SDK on the machine it was
written on, so every Dart file here is unverified — expect to fix a handful of
compile errors on the first `flutter run`. The Django API it talks to *is*
tested (35 tests covering auth, scoping, idempotency and every endpoint), so
when something does not work, the server is the half that has been checked.

## Building it

You need Flutter 3.19 or newer. From this directory:

```bash
flutter create --org com.lifemedpharmaceutical --platforms=android,ios .
flutter pub get
dart run flutter_launcher_icons            # app icon from the LifeMed mark
dart run flutter_native_splash:create      # splash from the full logo
flutter run
```

`flutter create .` in an existing directory fills in the native project folders
without touching `lib/`, `pubspec.yaml` or `assets/`.

**Do not leave off `--org`.** Without it the bundle identifier is
`com.example.…`, and no Apple account can be issued a provisioning profile for
a domain it does not own — iOS builds then fail at signing with "No profiles
for 'com.example.lifemedMr' were found". If it has already happened, fix it in
place rather than regenerating:

```bash
sed -i '' 's/com\.example\.lifemedMr/com.lifemedpharmaceutical.mr/g' \
  ios/Runner.xcodeproj/project.pbxproj
```

That rewrites six entries — three build configurations each for Runner and
RunnerTests.

## Getting it onto the team's phones

**Android is the path that matters.** The field team is on Android, and an APK
needs no store, no developer account and no per-device provisioning:

```bash
flutter build apk --release
# build/app/outputs/flutter-apk/app-release.apk
```

Send that file to the MRs directly. They allow "install from unknown sources"
once, and updates are just a newer APK.

**iOS is for your own testing.** It needs an Apple ID signed in under
Xcode → Settings → Accounts, and a bundle identifier on a domain you own (see
above). A free Apple ID runs the app on your own device with a profile that
expires weekly; putting it on anyone else's phone means the paid Developer
Program and TestFlight.

The server address is baked in at build time and defaults to the live site.
Point a test build somewhere else with:

```bash
flutter run --dart-define=API_BASE=http://10.0.2.2:8000/api/v1
```

(`10.0.2.2` is how the Android emulator reaches `localhost` on the host.)

## Before an MR can sign in

Two things have to be true on the server, both done from the web ERP:

1. **Team Management → Give Login** creates their account, sets the Field Staff
   role and links it to their employee record. All three matter; a login
   missing any of them either sees nothing or is refused at `/auth/login/`.
2. Their employee record needs a **territory**, or the app has no call points
   to show and cannot file new ones.

Targets are set at **Reports → Targets**. Without one the app still shows the
month's actuals, just with nothing to measure them against.

## How offline works

This is the part worth understanding before changing anything.

**Reads** are served from a local SQLite cache filled by `/bootstrap/` — the
MR's own territory, its call points and the doctors at each, the product list
and the expense categories. Opening the app with no signal shows the last
sync's data rather than an error, and the header says when that was.

**Writes never touch the network directly.** Recording a visit writes a row to
the `outbox` table and returns immediately; the screen closes, the scheduled
call ticks off, and `SyncService` pushes it whenever there is a line. An MR in
a hospital basement records calls exactly as they would on wifi.

Three things make that safe:

- **A `client_uuid` per queued write**, generated on the device. The server
  matches on it and returns the original record instead of creating a second,
  so a dropped reply or a retried queue cannot book the same call twice.
- **Ordered, one at a time.** A visit can name a doctor that was also created
  offline, so the doctor has to land first. The queue drains oldest-first and
  stops on a connection failure rather than skipping ahead.
- **Placeholder ids are persisted.** A record created offline gets a negative
  id, and the real one is written to `id_map` the moment the server replies —
  not at the end of the run, because the run is exactly what might not finish.
  In memory alone, killing the app between pushing a doctor and pushing their
  visit would strand that visit for good.

An item the server *refuses* (a call point outside the territory, say) is not
retried forever: after five attempts it is marked stuck, with the server's own
message stored against it.

## Layout

```
lib/
  main.dart              app entry, API_BASE
  theme.dart             colours sampled from the logo
  api/client.dart        HTTP, token storage, error shapes
  data/local_store.dart  SQLite cache + outbox + id map
  data/sync.dart         drains the outbox when there is signal
  models/models.dart     plain data classes, defensive parsing
  state/app_state.dart   the single ChangeNotifier the screens read
  screens/               login, home, schedule, record visit,
                         call points & doctors, performance, expenses
  widgets/common.dart    sync bar, stat tiles, empty states
```

`ApiException` and `OfflineException` are deliberately separate: no signal
means "queue it and carry on", a 400 means "this will never work, tell the
MR". Conflating them is how offline apps end up silently dropping work.

## Doctors move, they are not re-created

A call point is a *place*; a doctor is a *person* who currently sits there.
When a doctor leaves a hospital, use **Moved somewhere else** on their menu —
that records a move and carries their visit history with them. Adding them
again at the new address instead splits their history across two records and
the office cannot see that it is one person.
