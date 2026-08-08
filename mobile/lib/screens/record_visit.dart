import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../models/models.dart';
import '../state/app_state.dart';
import '../theme.dart';
import '../widgets/common.dart';

/// Recording a call. Never blocks on the network: the visit goes into the
/// outbox and the screen closes, whether or not there is a line.
class RecordVisitScreen extends StatefulWidget {
  const RecordVisitScreen({super.key, this.planVisit});

  final PlanVisit? planVisit;

  @override
  State<RecordVisitScreen> createState() => _RecordVisitScreenState();
}

class _RecordVisitScreenState extends State<RecordVisitScreen> {
  CallPoint? _callPoint;
  Doctor? _doctor;
  String _outcome = 'met';
  DateTime _date = DateTime.now();
  DateTime? _nextVisit;
  final _feedback = TextEditingController();
  final Set<int> _products = {};
  bool _saving = false;

  @override
  void initState() {
    super.initState();

    final scheduled = widget.planVisit;

    if (scheduled != null) {
      final points = context.read<AppState>().callPoints;

      _callPoint = points
          .where((c) => c.id == scheduled.callPointId)
          .cast<CallPoint?>()
          .firstWhere((c) => true, orElse: () => null);

      _date = scheduled.visitDate;
    }
  }

  @override
  void dispose() {
    _feedback.dispose();
    super.dispose();
  }

  Future<void> _save() async {
    if (_callPoint == null) {
      showMessage(context, 'Pick where you visited.', bad: true);

      return;
    }

    setState(() => _saving = true);

    await context.read<AppState>().recordVisit(
          callPointId: _callPoint!.id,
          doctorId: _doctor?.id,
          planVisitId: widget.planVisit?.id,
          visitDate: _date,
          outcome: _outcome,
          doctorName: _doctor?.name ?? '',
          speciality: _doctor?.speciality ?? '',
          feedback: _feedback.text.trim(),
          nextVisit: _nextVisit,
          productIds: _products.toList(),
        );

    if (!mounted) return;

    Navigator.of(context).pop();
    showMessage(context, 'Visit recorded.');
  }

  @override
  Widget build(BuildContext context) {
    final state = context.watch<AppState>();
    final doctors = _callPoint?.doctors ?? const <Doctor>[];

    return Scaffold(
      appBar: AppBar(title: const Text('Record a Visit')),
      body: ListView(
        padding: const EdgeInsets.all(16),
        children: [
          if (widget.planVisit != null)
            Container(
              padding: const EdgeInsets.all(12),
              margin: const EdgeInsets.only(bottom: 16),
              decoration: BoxDecoration(
                color: const Color(0xFFF0F6FB),
                borderRadius: BorderRadius.circular(10),
              ),
              child: Text(
                'Against your scheduled call on '
                '${widget.planVisit!.callPointName}. Saving ticks it off.',
                style: const TextStyle(fontSize: 13, color: brandDark),
              ),
            ),

          const _Label('Where'),
          DropdownButtonFormField<CallPoint>(
            initialValue: _callPoint,
            isExpanded: true,
            hint: const Text('Pick a call point'),
            items: [
              for (final point in state.callPoints)
                DropdownMenuItem(value: point, child: Text(point.name)),
            ],
            onChanged: (value) => setState(() {
              _callPoint = value;
              _doctor = null;
            }),
          ),
          const SizedBox(height: 16),

          const _Label('Doctor seen'),
          DropdownButtonFormField<Doctor>(
            initialValue: _doctor,
            isExpanded: true,
            hint: Text(
              doctors.isEmpty
                  ? 'No doctors listed here yet'
                  : 'Pick the doctor',
            ),
            items: [
              for (final doctor in doctors)
                DropdownMenuItem(
                  value: doctor,
                  child: Text(
                    doctor.speciality.isEmpty
                        ? doctor.name
                        : '${doctor.name} · ${doctor.speciality}',
                  ),
                ),
            ],
            onChanged: doctors.isEmpty
                ? null
                : (value) => setState(() => _doctor = value),
          ),
          if (_callPoint != null && doctors.isEmpty)
            const Padding(
              padding: EdgeInsets.only(top: 6),
              child: Text(
                'Add the doctor from the Doctors tab, or record the call '
                'against the place alone.',
                style: TextStyle(fontSize: 11.5, color: muted),
              ),
            ),
          const SizedBox(height: 16),

          const _Label('How did it go'),
          SegmentedButton<String>(
            segments: const [
              ButtonSegment(value: 'met', label: Text('Met')),
              ButtonSegment(value: 'not_available', label: Text('Not in')),
              ButtonSegment(value: 'rescheduled', label: Text('Rescheduled')),
            ],
            selected: {_outcome},
            onSelectionChanged: (choice) =>
                setState(() => _outcome = choice.first),
          ),
          const SizedBox(height: 16),

          const _Label('Date'),
          OutlinedButton.icon(
            style: OutlinedButton.styleFrom(
              minimumSize: const Size.fromHeight(50),
              alignment: Alignment.centerLeft,
            ),
            icon: const Icon(Icons.calendar_today_outlined, size: 18),
            label: Text(fullDate.format(_date)),
            onPressed: () async {
              final picked = await showDatePicker(
                context: context,
                initialDate: _date,
                // A visit cannot have happened next month, and back-dating
                // beyond a fortnight is nearly always a slip.
                firstDate: DateTime.now().subtract(const Duration(days: 14)),
                lastDate: DateTime.now(),
              );

              if (picked != null) setState(() => _date = picked);
            },
          ),
          const SizedBox(height: 16),

          const _Label('Products detailed'),
          Wrap(
            spacing: 8,
            runSpacing: 4,
            children: [
              for (final product in state.products)
                FilterChip(
                  label: Text(product.name),
                  selected: _products.contains(product.id),
                  onSelected: (on) => setState(() {
                    on ? _products.add(product.id) : _products.remove(product.id);
                  }),
                ),
            ],
          ),
          const SizedBox(height: 16),

          const _Label('Feedback'),
          TextField(
            controller: _feedback,
            maxLines: 3,
            decoration: const InputDecoration(
              hintText: 'What the doctor said, what to follow up on…',
            ),
          ),
          const SizedBox(height: 16),

          const _Label('Next visit'),
          OutlinedButton.icon(
            style: OutlinedButton.styleFrom(
              minimumSize: const Size.fromHeight(50),
              alignment: Alignment.centerLeft,
            ),
            icon: const Icon(Icons.event_outlined, size: 18),
            label: Text(
              _nextVisit == null ? 'Not set' : fullDate.format(_nextVisit!),
            ),
            onPressed: () async {
              final picked = await showDatePicker(
                context: context,
                initialDate: DateTime.now().add(const Duration(days: 14)),
                firstDate: DateTime.now(),
                lastDate: DateTime.now().add(const Duration(days: 365)),
              );

              if (picked != null) setState(() => _nextVisit = picked);
            },
          ),
          const SizedBox(height: 26),

          FilledButton.icon(
            onPressed: _saving ? null : _save,
            icon: const Icon(Icons.check),
            label: const Text('Save Visit'),
          ),
          const SizedBox(height: 10),
          const Text(
            'Saved on your phone straight away. It reaches the office by '
            'itself when you have signal.',
            textAlign: TextAlign.center,
            style: TextStyle(fontSize: 12, color: muted),
          ),
        ],
      ),
    );
  }
}

class _Label extends StatelessWidget {
  const _Label(this.text);

  final String text;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.only(bottom: 6),
      child: Text(
        text.toUpperCase(),
        style: const TextStyle(
          fontSize: 11,
          letterSpacing: 0.6,
          fontWeight: FontWeight.w700,
          color: muted,
        ),
      ),
    );
  }
}
