import 'package:flutter/material.dart';
import 'package:intl/intl.dart';

import '../theme.dart';

final money = NumberFormat('#,##0.00');
final dayMonth = DateFormat('d MMM');
final fullDate = DateFormat('EEEE, d MMM yyyy');
final monthYear = DateFormat('MMMM yyyy');

/// The strip under the app bar saying whether work is waiting to go up.
///
/// Deliberately always visible when something is queued: an MR needs to know
/// their calls have not reached the office before they finish for the day.
class SyncBar extends StatelessWidget {
  const SyncBar({super.key, required this.pending, required this.lastSynced});

  final int pending;
  final DateTime? lastSynced;

  @override
  Widget build(BuildContext context) {
    if (pending == 0) {
      if (lastSynced == null) return const SizedBox.shrink();

      return _bar(
        colour: const Color(0xFFEFF6EF),
        textColour: const Color(0xFF1E7B24),
        icon: Icons.cloud_done_outlined,
        text: 'All sent · updated ${DateFormat.Hm().format(lastSynced!)}',
      );
    }

    return _bar(
      colour: const Color(0xFFFDF8EC),
      textColour: const Color(0xFF8A6100),
      icon: Icons.cloud_upload_outlined,
      text: '$pending item${pending == 1 ? '' : 's'} waiting to send',
    );
  }

  Widget _bar({
    required Color colour,
    required Color textColour,
    required IconData icon,
    required String text,
  }) {
    return Container(
      width: double.infinity,
      padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 8),
      color: colour,
      child: Row(
        children: [
          Icon(icon, size: 16, color: textColour),
          const SizedBox(width: 8),
          Text(
            text,
            style: TextStyle(
              color: textColour,
              fontSize: 12.5,
              fontWeight: FontWeight.w500,
            ),
          ),
        ],
      ),
    );
  }
}

class StatTile extends StatelessWidget {
  const StatTile({
    super.key,
    required this.label,
    required this.value,
    this.note,
    this.accent = brandDark,
  });

  final String label;
  final String value;
  final String? note;
  final Color accent;

  @override
  Widget build(BuildContext context) {
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(14),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          mainAxisSize: MainAxisSize.min,
          children: [
            Text(
              label.toUpperCase(),
              style: const TextStyle(
                fontSize: 10.5,
                letterSpacing: 0.6,
                color: muted,
                fontWeight: FontWeight.w600,
              ),
            ),
            const SizedBox(height: 6),
            Text(
              value,
              style: TextStyle(
                fontSize: 22,
                fontWeight: FontWeight.bold,
                color: accent,
              ),
            ),
            if (note != null) ...[
              const SizedBox(height: 2),
              Text(note!, style: const TextStyle(fontSize: 11, color: muted)),
            ],
          ],
        ),
      ),
    );
  }
}

/// What to show when a list is empty, in words that say what to do next.
class EmptyState extends StatelessWidget {
  const EmptyState({
    super.key,
    required this.icon,
    required this.title,
    required this.message,
    this.action,
  });

  final IconData icon;
  final String title;
  final String message;
  final Widget? action;

  @override
  Widget build(BuildContext context) {
    return Center(
      child: Padding(
        padding: const EdgeInsets.all(32),
        child: Column(
          mainAxisAlignment: MainAxisAlignment.center,
          children: [
            Icon(icon, size: 46, color: muted.withValues(alpha: 0.5)),
            const SizedBox(height: 14),
            Text(
              title,
              style: const TextStyle(
                fontSize: 16,
                fontWeight: FontWeight.w600,
                color: brandDark,
              ),
            ),
            const SizedBox(height: 6),
            Text(
              message,
              textAlign: TextAlign.center,
              style: const TextStyle(color: muted, fontSize: 13.5),
            ),
            if (action != null) ...[const SizedBox(height: 18), action!],
          ],
        ),
      ),
    );
  }
}

void showMessage(BuildContext context, String message, {bool bad = false}) {
  ScaffoldMessenger.of(context)
    ..hideCurrentSnackBar()
    ..showSnackBar(
      SnackBar(
        content: Text(message),
        backgroundColor: bad ? const Color(0xFFA71D2A) : brandDark,
        behavior: SnackBarBehavior.floating,
      ),
    );
}
