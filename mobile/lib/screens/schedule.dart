import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../models/models.dart';
import '../state/app_state.dart';
import '../theme.dart';
import '../widgets/common.dart';
import 'record_visit.dart';

const _dayNames = [
  'Monday',
  'Tuesday',
  'Wednesday',
  'Thursday',
  'Friday',
  'Saturday',
];

class ScheduleScreen extends StatefulWidget {
  const ScheduleScreen({super.key});

  @override
  State<ScheduleScreen> createState() => _ScheduleScreenState();
}

class _ScheduleScreenState extends State<ScheduleScreen> {
  late DateTime _week = mondayOf(DateTime.now());
  bool _building = false;

  Future<void> _show(DateTime week) async {
    setState(() => _week = week);

    await context.read<AppState>().loadSchedule(week);
  }

  Future<void> _build() async {
    setState(() => _building = true);

    final error = await context.read<AppState>().generateSchedule(_week);

    if (!mounted) return;

    setState(() => _building = false);

    if (error != null) {
      showMessage(context, error, bad: true);
    } else {
      showMessage(context, 'Week filled from your territory.');
    }
  }

  @override
  Widget build(BuildContext context) {
    final state = context.watch<AppState>();
    final plan = state.plan;
    final today = DateTime.now();

    return Scaffold(
      appBar: AppBar(
        title: const Text('My Schedule'),
        actions: [
          IconButton(
            icon: const Icon(Icons.chevron_left),
            onPressed: () => _show(_week.subtract(const Duration(days: 7))),
          ),
          IconButton(
            icon: const Icon(Icons.chevron_right),
            onPressed: () => _show(_week.add(const Duration(days: 7))),
          ),
        ],
      ),
      body: Column(
        children: [
          SyncBar(pending: state.pending, lastSynced: state.lastSynced),

          Container(
            width: double.infinity,
            padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 12),
            color: Colors.white,
            child: Row(
              mainAxisAlignment: MainAxisAlignment.spaceBetween,
              children: [
                Text(
                  '${dayMonth.format(_week)} – '
                  '${dayMonth.format(_week.add(const Duration(days: 5)))}',
                  style: const TextStyle(
                    fontWeight: FontWeight.w600,
                    color: brandDark,
                  ),
                ),
                if (plan != null)
                  Pill(
                    plan.status,
                    color: plan.status == 'approved' ? brandGreen : muted,
                  ),
              ],
            ),
          ),

          Expanded(
            child: plan == null
                ? EmptyState(
                    icon: Icons.calendar_month_outlined,
                    title: 'No plan for this week',
                    message: 'Build one from your territory — the doctors you '
                        'have left longest come first.',
                    action: FilledButton.icon(
                      onPressed: _building ? null : _build,
                      icon: _building
                          ? const SizedBox(
                              height: 16,
                              width: 16,
                              child: CircularProgressIndicator(
                                strokeWidth: 2,
                                color: Colors.white,
                              ),
                            )
                          : const Icon(Icons.auto_awesome),
                      label: const Text('Build this week'),
                    ),
                  )
                : ListView.builder(
                    padding: const EdgeInsets.fromLTRB(16, 12, 16, 30),
                    itemCount: _dayNames.length,
                    itemBuilder: (context, day) {
                      final date = _week.add(Duration(days: day));
                      final visits = plan.onDay(day);
                      final done = visits.where((v) => v.isDone).length;

                      final isToday = date.year == today.year &&
                          date.month == today.month &&
                          date.day == today.day;

                      return _DayCard(
                        name: _dayNames[day],
                        date: date,
                        visits: visits,
                        done: done,
                        isToday: isToday,
                      );
                    },
                  ),
          ),
        ],
      ),
    );
  }
}

class _DayCard extends StatelessWidget {
  const _DayCard({
    required this.name,
    required this.date,
    required this.visits,
    required this.done,
    required this.isToday,
  });

  final String name;
  final DateTime date;
  final List<PlanVisit> visits;
  final int done;
  final bool isToday;

  @override
  Widget build(BuildContext context) {
    return Card(
      shape: RoundedRectangleBorder(
        borderRadius: BorderRadius.circular(12),
        side: BorderSide(
          color: isToday ? brandBlue : const Color(0xFFE4EAF1),
          width: isToday ? 1.6 : 1,
        ),
      ),
      child: Padding(
        padding: const EdgeInsets.all(14),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              mainAxisAlignment: MainAxisAlignment.spaceBetween,
              children: [
                Text(
                  '$name · ${dayMonth.format(date)}',
                  style: const TextStyle(
                    fontWeight: FontWeight.w700,
                    color: brandDark,
                  ),
                ),
                Text(
                  '$done / ${visits.length}',
                  style: TextStyle(
                    fontWeight: FontWeight.bold,
                    color: visits.isNotEmpty && done >= visits.length
                        ? brandGreen
                        : muted,
                  ),
                ),
              ],
            ),
            const Divider(height: 18),

            if (visits.isEmpty)
              const Text(
                'No calls planned.',
                style: TextStyle(color: muted, fontSize: 13),
              )
            else
              ...visits.map(
                (visit) => Padding(
                  padding: const EdgeInsets.only(bottom: 6),
                  child: Row(
                    children: [
                      Icon(
                        visit.isDone
                            ? Icons.check_circle
                            : Icons.radio_button_unchecked,
                        size: 18,
                        color: visit.isDone ? brandGreen : muted,
                      ),
                      const SizedBox(width: 9),
                      Expanded(
                        child: Text(
                          visit.callPointName,
                          style: TextStyle(
                            fontSize: 13.5,
                            color: visit.isDone ? muted : Colors.black87,
                            decoration: visit.isDone
                                ? TextDecoration.lineThrough
                                : null,
                          ),
                        ),
                      ),
                      if (!visit.isDone)
                        TextButton(
                          style: TextButton.styleFrom(
                            visualDensity: VisualDensity.compact,
                          ),
                          onPressed: () => Navigator.of(context).push(
                            MaterialPageRoute(
                              builder: (_) =>
                                  RecordVisitScreen(planVisit: visit),
                            ),
                          ),
                          child: const Text('Record'),
                        ),
                    ],
                  ),
                ),
              ),
          ],
        ),
      ),
    );
  }
}
