import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../models/models.dart';
import '../state/app_state.dart';
import '../theme.dart';
import '../widgets/common.dart';
import 'call_points.dart';
import 'expenses.dart';
import 'performance.dart';
import 'record_visit.dart';
import 'schedule.dart';

class HomeScreen extends StatefulWidget {
  const HomeScreen({super.key});

  @override
  State<HomeScreen> createState() => _HomeScreenState();
}

class _HomeScreenState extends State<HomeScreen> {
  int _tab = 0;

  @override
  Widget build(BuildContext context) {
    final pages = const [
      _TodayPage(),
      ScheduleScreen(),
      CallPointsScreen(),
      PerformanceScreen(),
    ];

    return Scaffold(
      body: pages[_tab],
      bottomNavigationBar: NavigationBar(
        selectedIndex: _tab,
        onDestinationSelected: (index) => setState(() => _tab = index),
        destinations: const [
          NavigationDestination(
            icon: Icon(Icons.today_outlined),
            selectedIcon: Icon(Icons.today),
            label: 'Today',
          ),
          NavigationDestination(
            icon: Icon(Icons.calendar_month_outlined),
            selectedIcon: Icon(Icons.calendar_month),
            label: 'Schedule',
          ),
          NavigationDestination(
            icon: Icon(Icons.location_on_outlined),
            selectedIcon: Icon(Icons.location_on),
            label: 'Doctors',
          ),
          NavigationDestination(
            icon: Icon(Icons.insights_outlined),
            selectedIcon: Icon(Icons.insights),
            label: 'Performance',
          ),
        ],
      ),
    );
  }
}

class _TodayPage extends StatelessWidget {
  const _TodayPage();

  @override
  Widget build(BuildContext context) {
    final state = context.watch<AppState>();
    final today = DateTime.now();
    final weekday = today.weekday - 1;

    final scheduled = state.plan?.onDay(weekday) ?? const <PlanVisit>[];
    final done = scheduled.where((v) => v.isDone).length;

    return Scaffold(
      appBar: AppBar(
        title: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(
              state.employee?.fullName ?? 'My Day',
              style: const TextStyle(fontSize: 17),
            ),
            Text(
              state.employee?.territoryName ?? '',
              style: const TextStyle(fontSize: 12, color: Colors.white70),
            ),
          ],
        ),
        actions: [
          IconButton(
            icon: const Icon(Icons.receipt_long_outlined),
            tooltip: 'Expenses',
            onPressed: () => Navigator.of(context).push(
              MaterialPageRoute(builder: (_) => const ExpensesScreen()),
            ),
          ),
          PopupMenuButton<String>(
            onSelected: (choice) async {
              if (choice == 'refresh') {
                await context.read<AppState>().refresh();
              } else if (choice == 'signout') {
                await context.read<AppState>().signOut();
              }
            },
            itemBuilder: (_) => const [
              PopupMenuItem(value: 'refresh', child: Text('Sync now')),
              PopupMenuItem(value: 'signout', child: Text('Sign out')),
            ],
          ),
        ],
      ),
      body: RefreshIndicator(
        onRefresh: () => context.read<AppState>().refresh(),
        child: ListView(
          padding: const EdgeInsets.only(bottom: 90),
          children: [
            SyncBar(pending: state.pending, lastSynced: state.lastSynced),

            Padding(
              padding: const EdgeInsets.fromLTRB(16, 16, 16, 4),
              child: Text(
                fullDate.format(today),
                style: const TextStyle(
                  fontSize: 15,
                  fontWeight: FontWeight.w600,
                  color: brandDark,
                ),
              ),
            ),

            Padding(
              padding: const EdgeInsets.symmetric(horizontal: 16),
              child: Row(
                children: [
                  Expanded(
                    child: StatTile(
                      label: 'Calls today',
                      value: '$done / ${scheduled.length}',
                      accent: done >= scheduled.length && scheduled.isNotEmpty
                          ? brandGreen
                          : brandDark,
                    ),
                  ),
                  const SizedBox(width: 10),
                  Expanded(
                    child: StatTile(
                      label: 'This month',
                      value: '${state.performance?.callCount ?? 0}',
                      note: 'calls made',
                    ),
                  ),
                ],
              ),
            ),

            Padding(
              padding: const EdgeInsets.fromLTRB(16, 18, 16, 8),
              child: Row(
                mainAxisAlignment: MainAxisAlignment.spaceBetween,
                children: [
                  const Text(
                    "Today's Calls",
                    style: TextStyle(
                      fontSize: 15,
                      fontWeight: FontWeight.w700,
                      color: brandDark,
                    ),
                  ),
                  if (state.plan == null)
                    TextButton(
                      onPressed: () => Navigator.of(context).push(
                        MaterialPageRoute(
                          builder: (_) => const ScheduleScreen(),
                        ),
                      ),
                      child: const Text('Build a week'),
                    ),
                ],
              ),
            ),

            if (scheduled.isEmpty)
              const Padding(
                padding: EdgeInsets.symmetric(horizontal: 16, vertical: 30),
                child: EmptyState(
                  icon: Icons.event_available_outlined,
                  title: 'Nothing scheduled today',
                  message: 'Anything you visit still counts — record it and it '
                      'shows up as an unplanned call.',
                ),
              )
            else
              ...scheduled.map(
                (visit) => Padding(
                  padding: const EdgeInsets.symmetric(horizontal: 16),
                  child: _VisitCard(visit: visit),
                ),
              ),
          ],
        ),
      ),
      floatingActionButton: FloatingActionButton.extended(
        onPressed: () => Navigator.of(context).push(
          MaterialPageRoute(builder: (_) => const RecordVisitScreen()),
        ),
        icon: const Icon(Icons.add),
        label: const Text('Record Visit'),
      ),
    );
  }
}

class _VisitCard extends StatelessWidget {
  const _VisitCard({required this.visit});

  final PlanVisit visit;

  @override
  Widget build(BuildContext context) {
    return Card(
      child: ListTile(
        contentPadding: const EdgeInsets.symmetric(horizontal: 14, vertical: 6),
        title: Text(
          visit.callPointName,
          style: const TextStyle(fontWeight: FontWeight.w600, fontSize: 15),
        ),
        subtitle: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            if (visit.address.isNotEmpty)
              Text(visit.address, style: const TextStyle(fontSize: 12.5)),
            if (visit.objective.isNotEmpty)
              Text(
                visit.objective,
                style: const TextStyle(fontSize: 12, color: muted),
              ),
          ],
        ),
        trailing: visit.isDone
            ? const Pill('visited', color: brandGreen)
            : FilledButton.tonal(
                style: FilledButton.styleFrom(
                  minimumSize: const Size(84, 38),
                ),
                onPressed: () => Navigator.of(context).push(
                  MaterialPageRoute(
                    builder: (_) => RecordVisitScreen(planVisit: visit),
                  ),
                ),
                child: const Text('Record'),
              ),
      ),
    );
  }
}
