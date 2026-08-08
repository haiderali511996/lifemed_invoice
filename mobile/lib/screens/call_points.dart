import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../models/models.dart';
import '../state/app_state.dart';
import '../theme.dart';
import '../widgets/common.dart';

/// The MR's own patch: the places they call on, and the doctors at each.
class CallPointsScreen extends StatefulWidget {
  const CallPointsScreen({super.key});

  @override
  State<CallPointsScreen> createState() => _CallPointsScreenState();
}

class _CallPointsScreenState extends State<CallPointsScreen> {
  String _query = '';

  @override
  Widget build(BuildContext context) {
    final state = context.watch<AppState>();

    final needle = _query.trim().toLowerCase();

    final points = needle.isEmpty
        ? state.callPoints
        : state.callPoints.where((point) {
            if (point.name.toLowerCase().contains(needle)) return true;

            return point.doctors
                .any((d) => d.name.toLowerCase().contains(needle));
          }).toList();

    return Scaffold(
      appBar: AppBar(
        title: const Text('Doctors & Call Points'),
        bottom: PreferredSize(
          preferredSize: const Size.fromHeight(58),
          child: Padding(
            padding: const EdgeInsets.fromLTRB(12, 0, 12, 10),
            child: TextField(
              onChanged: (value) => setState(() => _query = value),
              decoration: const InputDecoration(
                hintText: 'Search a doctor or a place',
                prefixIcon: Icon(Icons.search),
                isDense: true,
              ),
            ),
          ),
        ),
      ),
      body: Column(
        children: [
          SyncBar(pending: state.pending, lastSynced: state.lastSynced),
          Expanded(
            child: points.isEmpty
                ? const EmptyState(
                    icon: Icons.location_off_outlined,
                    title: 'Nothing here yet',
                    message: 'Add the clinics and hospitals you call on, then '
                        'the doctors who sit in them.',
                  )
                : ListView.builder(
                    padding: const EdgeInsets.fromLTRB(12, 8, 12, 90),
                    itemCount: points.length,
                    itemBuilder: (context, index) =>
                        _CallPointCard(point: points[index]),
                  ),
          ),
        ],
      ),
      floatingActionButton: FloatingActionButton.extended(
        onPressed: () => _addCallPoint(context),
        icon: const Icon(Icons.add_location_alt_outlined),
        label: const Text('Add Place'),
      ),
    );
  }
}

class _CallPointCard extends StatelessWidget {
  const _CallPointCard({required this.point});

  final CallPoint point;

  @override
  Widget build(BuildContext context) {
    return Card(
      child: ExpansionTile(
        shape: const Border(),
        title: Text(
          point.name,
          style: const TextStyle(fontWeight: FontWeight.w600, fontSize: 15),
        ),
        subtitle: Text(
          [
            point.kind,
            if (point.address.isNotEmpty) point.address,
            '${point.doctors.length} doctor${point.doctors.length == 1 ? '' : 's'}',
          ].join(' · '),
          style: const TextStyle(fontSize: 12, color: muted),
        ),
        childrenPadding: const EdgeInsets.fromLTRB(16, 0, 8, 10),
        children: [
          for (final doctor in point.doctors)
            ListTile(
              dense: true,
              contentPadding: EdgeInsets.zero,
              leading: const Icon(Icons.person_outline, size: 20),
              title: Text(doctor.name, style: const TextStyle(fontSize: 14)),
              subtitle: Text(
                [
                  if (doctor.speciality.isNotEmpty) doctor.speciality,
                  if (doctor.qualification.isNotEmpty) doctor.qualification,
                  if (doctor.phone.isNotEmpty) doctor.phone,
                ].join(' · '),
                style: const TextStyle(fontSize: 11.5),
              ),
              trailing: PopupMenuButton<String>(
                icon: const Icon(Icons.more_vert, size: 20),
                onSelected: (choice) {
                  if (choice == 'edit') {
                    _editDoctor(context, doctor);
                  } else if (choice == 'move') {
                    _moveDoctor(context, doctor);
                  }
                },
                itemBuilder: (_) => const [
                  PopupMenuItem(value: 'edit', child: Text('Edit details')),
                  PopupMenuItem(
                    value: 'move',
                    child: Text('Moved somewhere else'),
                  ),
                ],
              ),
            ),

          Align(
            alignment: Alignment.centerLeft,
            child: TextButton.icon(
              onPressed: () => _addDoctor(context, point),
              icon: const Icon(Icons.person_add_alt, size: 18),
              label: const Text('Add a doctor here'),
            ),
          ),
        ],
      ),
    );
  }
}

// ------------------------------------------------------------------ sheets

Future<void> _addCallPoint(BuildContext context) async {
  final name = TextEditingController();
  final address = TextEditingController();
  final phone = TextEditingController();
  var kind = 'doctor';

  await _sheet(
    context,
    title: 'New Call Point',
    note: 'Added to your own territory.',
    builder: (setSheetState) => [
      TextField(
        controller: name,
        autofocus: true,
        decoration: const InputDecoration(labelText: 'Name'),
      ),
      const SizedBox(height: 12),
      SegmentedButton<String>(
        segments: const [
          ButtonSegment(value: 'doctor', label: Text('Clinic')),
          ButtonSegment(value: 'hospital', label: Text('Hospital')),
          ButtonSegment(value: 'chemist', label: Text('Chemist')),
        ],
        selected: {kind},
        onSelectionChanged: (choice) =>
            setSheetState(() => kind = choice.first),
      ),
      const SizedBox(height: 12),
      TextField(
        controller: address,
        decoration: const InputDecoration(labelText: 'Address'),
      ),
      const SizedBox(height: 12),
      TextField(
        controller: phone,
        keyboardType: TextInputType.phone,
        decoration: const InputDecoration(labelText: 'Phone'),
      ),
    ],
    onSave: (ctx) async {
      if (name.text.trim().isEmpty) return 'Give it a name.';

      await ctx.read<AppState>().addCallPoint(
            name: name.text.trim(),
            kind: kind,
            address: address.text.trim(),
            phone: phone.text.trim(),
          );

      return null;
    },
  );
}

Future<void> _addDoctor(BuildContext context, CallPoint point) async {
  final name = TextEditingController();
  final speciality = TextEditingController();
  final qualification = TextEditingController();
  final phone = TextEditingController();
  var potential = 'medium';

  await _sheet(
    context,
    title: 'New Doctor at ${point.name}',
    builder: (setSheetState) => [
      TextField(
        controller: name,
        autofocus: true,
        decoration: const InputDecoration(labelText: 'Name'),
      ),
      const SizedBox(height: 12),
      TextField(
        controller: speciality,
        decoration: const InputDecoration(labelText: 'Speciality'),
      ),
      const SizedBox(height: 12),
      TextField(
        controller: qualification,
        decoration: const InputDecoration(labelText: 'Qualification'),
      ),
      const SizedBox(height: 12),
      TextField(
        controller: phone,
        keyboardType: TextInputType.phone,
        decoration: const InputDecoration(labelText: 'Phone'),
      ),
      const SizedBox(height: 12),
      SegmentedButton<String>(
        segments: const [
          ButtonSegment(value: 'high', label: Text('High')),
          ButtonSegment(value: 'medium', label: Text('Medium')),
          ButtonSegment(value: 'low', label: Text('Low')),
        ],
        selected: {potential},
        onSelectionChanged: (choice) =>
            setSheetState(() => potential = choice.first),
      ),
    ],
    onSave: (ctx) async {
      if (name.text.trim().isEmpty) return 'Give the doctor a name.';

      await ctx.read<AppState>().addDoctor(
            callPointId: point.id,
            name: name.text.trim(),
            speciality: speciality.text.trim(),
            qualification: qualification.text.trim(),
            phone: phone.text.trim(),
            potential: potential,
          );

      return null;
    },
  );
}

Future<void> _editDoctor(BuildContext context, Doctor doctor) async {
  final name = TextEditingController(text: doctor.name);
  final speciality = TextEditingController(text: doctor.speciality);
  final qualification = TextEditingController(text: doctor.qualification);
  final phone = TextEditingController(text: doctor.phone);

  await _sheet(
    context,
    title: 'Edit ${doctor.name}',
    builder: (_) => [
      TextField(
        controller: name,
        decoration: const InputDecoration(labelText: 'Name'),
      ),
      const SizedBox(height: 12),
      TextField(
        controller: speciality,
        decoration: const InputDecoration(labelText: 'Speciality'),
      ),
      const SizedBox(height: 12),
      TextField(
        controller: qualification,
        decoration: const InputDecoration(labelText: 'Qualification'),
      ),
      const SizedBox(height: 12),
      TextField(
        controller: phone,
        keyboardType: TextInputType.phone,
        decoration: const InputDecoration(labelText: 'Phone'),
      ),
    ],
    onSave: (ctx) async {
      if (name.text.trim().isEmpty) return 'A doctor needs a name.';

      await ctx.read<AppState>().updateDoctor(doctor, {
        'name': name.text.trim(),
        'speciality': speciality.text.trim(),
        'qualification': qualification.text.trim(),
        'phone': phone.text.trim(),
      });

      return null;
    },
  );
}

/// The doctor has left. Moves the person, keeping their visit history.
Future<void> _moveDoctor(BuildContext context, Doctor doctor) async {
  final reason = TextEditingController();
  CallPoint? destination;

  await _sheet(
    context,
    title: '${doctor.name} has moved',
    note: 'Their visit history moves with them — do not add them again at the '
        'new place.',
    builder: (setSheetState) {
      final options = context
          .read<AppState>()
          .callPoints
          .where((c) => c.id != doctor.callPointId)
          .toList();

      return [
        DropdownButtonFormField<CallPoint>(
          initialValue: destination,
          isExpanded: true,
          hint: const Text('Where are they now?'),
          items: [
            for (final point in options)
              DropdownMenuItem(value: point, child: Text(point.name)),
          ],
          onChanged: (value) => setSheetState(() => destination = value),
        ),
        const SizedBox(height: 12),
        TextField(
          controller: reason,
          decoration: const InputDecoration(
            labelText: 'Reason (optional)',
            hintText: 'Left the hospital, opened own clinic…',
          ),
        ),
      ];
    },
    onSave: (ctx) async {
      if (destination == null) return 'Pick where they moved to.';

      await ctx.read<AppState>().moveDoctor(
            doctor,
            destination!.id,
            reason: reason.text.trim(),
          );

      return null;
    },
  );
}

/// A bottom sheet with a save button, used by all four forms above.
///
/// `onSave` returns an error string to keep the sheet open, or null to close.
Future<void> _sheet(
  BuildContext context, {
  required String title,
  String? note,
  required List<Widget> Function(void Function(void Function())) builder,
  required Future<String?> Function(BuildContext) onSave,
}) {
  return showModalBottomSheet<void>(
    context: context,
    isScrollControlled: true,
    backgroundColor: Colors.white,
    shape: const RoundedRectangleBorder(
      borderRadius: BorderRadius.vertical(top: Radius.circular(18)),
    ),
    builder: (sheetContext) => StatefulBuilder(
      builder: (sheetContext, setSheetState) => Padding(
        padding: EdgeInsets.only(
          left: 20,
          right: 20,
          top: 20,
          bottom: MediaQuery.of(sheetContext).viewInsets.bottom + 20,
        ),
        child: SingleChildScrollView(
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              Text(
                title,
                style: const TextStyle(
                  fontSize: 17,
                  fontWeight: FontWeight.w700,
                  color: brandDark,
                ),
              ),
              if (note != null) ...[
                const SizedBox(height: 6),
                Text(
                  note,
                  style: const TextStyle(fontSize: 12.5, color: muted),
                ),
              ],
              const SizedBox(height: 18),
              ...builder(setSheetState),
              const SizedBox(height: 22),
              FilledButton(
                onPressed: () async {
                  final error = await onSave(sheetContext);

                  if (!sheetContext.mounted) return;

                  if (error != null) {
                    showMessage(sheetContext, error, bad: true);
                  } else {
                    Navigator.of(sheetContext).pop();
                  }
                },
                child: const Text('Save'),
              ),
            ],
          ),
        ),
      ),
    ),
  );
}
