# LifeMed Pharma — MR App

The field app: an MR's day, their doctors, their targets and their calls.

Compiled clean against **Flutter 3.35.4** — `flutter analyze` reports no
issues and `flutter build bundle --release` succeeds. It has not been run on a
real device or driven through a screen, so expect layout and behaviour to need
adjusting; what is verified is that it builds.

The Django API it talks to is covered by 35 tests — auth, territory scoping,
idempotent writes and every endpoint.

## Building it

You need Flutter 3.19 or newer. From this directory:

```bash
flutter create --org com.lifemedpharmaceutical --platforms=android,ios .
./tool/setup_android.sh                    # INTERNET permission — see below
flutter pub get
dart run flutter_launcher_icons            # app icon from the LifeMed mark
dart run flutter_native_splash:create      # splash from the full logo
flutter run
```

`flutter create .` in an existing directory fills in the native project folders
without touching `lib/`, `pubspec.yaml` or `assets/`.

**Then run `./tool/setup_android.sh`.** `flutter create` puts the INTERNET
permission in the debug and profile manifests only, never the main one — so a
debug build has network and a release build silently does not. The symptom is
"No connection" on a phone with full signal. `android/` is not tracked in git,
so this has to be re-applied after any `flutter create`; running it twice is
safe.

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

## The bundled certificate authority

`assets/ssl-com-chain.pem` carries SSL.com's 2022 root and the intermediate
that signed the server certificate, and `buildHttpClient()` adds them to the
roots the app already trusts.

Two real problems this solves. Android keeps its trust store in the system
image, so a phone that has not had a platform update since before that root
was published does not have it — and plenty of field phones haven't. And the
server currently sends only its leaf certificate without the intermediate, so
a client that cannot fetch the missing piece itself has no chain to validate.
Either one produces `CERTIFICATE_VERIFY_FAILED: unable to get local issuer
certificate` on a real device while working perfectly in an emulator.

It **adds** a certificate authority; it does not disable verification. A
forged or expired certificate is still rejected and every other host is
validated as before. Turning verification off would have been the easy fix
and the wrong one — it would leave every MR's credentials readable on any
hotel wifi.

Replacing the server certificate with one from a different CA means replacing
this file too. Fetch the new root and intermediate from the AIA URLs printed
by `openssl s_client -showcerts` against the server.

## Orders are requests, not sales

The Orders tab sends what a pharmacy wants to the office. It reserves no
stock and fixes no price: the office prices it, picks the batches and raises
the invoice, and *that* is what moves stock and creates a ledger entry. The
wording on the screen says so, because an MR who believes stock is held will
promise a delivery date the office cannot keep.

Orders queue like everything else, so one can be taken in a pharmacy with no
signal. Until it syncs it shows as "not sent yet" rather than pretending it
has reached anyone.

## Doctors move, they are not re-created

A call point is a *place*; a doctor is a *person* who currently sits there.
When a doctor leaves a hospital, use **Moved somewhere else** on their menu —
that records a move and carries their visit history with them. Adding them
again at the new address instead splits their history across two records and
the office cannot see that it is one person.
