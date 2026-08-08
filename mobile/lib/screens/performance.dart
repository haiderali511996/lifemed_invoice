import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../models/models.dart';
import '../state/app_state.dart';
import '../theme.dart';
import '../widgets/common.dart';

class PerformanceScreen extends StatelessWidget {
  const PerformanceScreen({super.key});

  @override
  Widget build(BuildContext context) {
    final state = context.watch<AppState>();
    final figures = state.performance;

    return Scaffold(
      appBar: AppBar(title: const Text('My Performance')),
      body: RefreshIndicator(
        onRefresh: () => context.read<AppState>().loadPerformance(),
        child: ListView(
          padding: const EdgeInsets.only(bottom: 30),
          children: [
            SyncBar(pending: state.pending, lastSynced: state.lastSynced),

            if (figures == null)
              const Padding(
                padding: EdgeInsets.only(top: 60),
                child: EmptyState(
                  icon: Icons.insights_outlined,
                  title: 'No figures yet',
                  message: 'Pull down to fetch this month once you have a '
                      'connection.',
                ),
              )
            else ...[
              Padding(
                padding: const EdgeInsets.fromLTRB(16, 16, 16, 6),
                child: Text(
                  monthYear.format(figures.month),
                  style: const TextStyle(
                    fontSize: 15,
                    fontWeight: FontWeight.w700,
                    color: brandDark,
                  ),
                ),
              ),

              Padding(
                padding: const EdgeInsets.symmetric(horizontal: 16),
                child: Column(
                  children: [
                    Row(
                      children: [
                        Expanded(
                          child: StatTile(
                            label: 'Net sales',
                            value: money.format(figures.sales),
                            note: 'after discounts, less returns',
                          ),
                        ),
                        const SizedBox(width: 10),
                        Expanded(
                          child: StatTile(
                            label: 'Commission',
                            value: money.format(figures.commission),
                            note: figures.commissionPercent > 0
                                ? '@ ${figures.commissionPercent.toStringAsFixed(0)}%'
                                : 'salary only',
                            accent: brandGreen,
                          ),
                        ),
                      ],
                    ),
                    Row(
                      children: [
                        Expanded(
                          child: StatTile(
                            label: 'Calls',
                            value: '${figures.callCount}',
                            note: '${figures.metCount} doctors met',
                          ),
                        ),
                        const SizedBox(width: 10),
                        Expanded(
                          child: StatTile(
                            label: 'Doctors seen',
                            value: '${figures.doctorCount}',
                            note: 'different people',
                          ),
                        ),
                      ],
                    ),
                  ],
                ),
              ),

              const Padding(
                padding: EdgeInsets.fromLTRB(16, 20, 16, 8),
                child: Text(
                  'Against Target',
                  style: TextStyle(
                    fontSize: 15,
                    fontWeight: FontWeight.w700,
                    color: brandDark,
                  ),
                ),
              ),

              if (!figures.hasTarget)
                const Padding(
                  padding: EdgeInsets.symmetric(horizontal: 16),
                  child: Card(
                    child: Padding(
                      padding: EdgeInsets.all(16),
                      child: Text(
                        'No target set for this month. The office sets these '
                        'in Team Management.',
                        style: TextStyle(color: muted, fontSize: 13.5),
                      ),
                    ),
                  ),
                )
              else
                ...figures.measures.map(
                  (measure) => Padding(
                    padding: const EdgeInsets.symmetric(horizontal: 16),
                    child: _MeasureBar(measure: measure),
                  ),
                ),
            ],
          ],
        ),
      ),
    );
  }
}

class _MeasureBar extends StatelessWidget {
  const _MeasureBar({required this.measure});

  final Measure measure;

  Color get _colour {
    if (measure.percent >= 100) return brandGreen;
    if (measure.percent >= 60) return const Color(0xFFE8A33D);

    return const Color(0xFFDC3545);
  }

  @override
  Widget build(BuildContext context) {
    final actual = measure.isMoney
        ? money.format(measure.actual)
        : measure.actual.toStringAsFixed(0);
    final target = measure.isMoney
        ? money.format(measure.target)
        : measure.target.toStringAsFixed(0);

    return Card(
      child: Padding(
        padding: const EdgeInsets.all(14),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              mainAxisAlignment: MainAxisAlignment.spaceBetween,
              children: [
                Text(
                  measure.label,
                  style: const TextStyle(
                    fontWeight: FontWeight.w600,
                    fontSize: 14,
                  ),
                ),
                Pill('${measure.percent}%', color: _colour),
              ],
            ),
            const SizedBox(height: 10),
            ClipRRect(
              borderRadius: BorderRadius.circular(6),
              child: LinearProgressIndicator(
                // Capped so a blowout does not run off the card; the number
                // beside it still says 140%.
                value: (measure.percent / 100).clamp(0.0, 1.0),
                minHeight: 8,
                backgroundColor: const Color(0xFFEDF1F5),
                valueColor: AlwaysStoppedAnimation(_colour),
              ),
            ),
            const SizedBox(height: 7),
            Text(
              '$actual of $target',
              style: const TextStyle(fontSize: 12, color: muted),
            ),
          ],
        ),
      ),
    );
  }
}
