import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../models/models.dart';
import '../state/app_state.dart';
import '../theme.dart';
import '../widgets/common.dart';

class ExpensesScreen extends StatefulWidget {
  const ExpensesScreen({super.key});

  @override
  State<ExpensesScreen> createState() => _ExpensesScreenState();
}

class _ExpensesScreenState extends State<ExpensesScreen> {
  List<dynamic> _claims = [];
  bool _loading = true;

  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load() async {
    try {
      final claims = await context.read<AppState>().api.expenses();

      if (mounted) setState(() => _claims = claims);
    } catch (_) {
      // Offline: the queued ones below still show what is waiting.
    } finally {
      if (mounted) setState(() => _loading = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final state = context.watch<AppState>();

    return Scaffold(
      appBar: AppBar(title: const Text('My Expenses')),
      body: Column(
        children: [
          SyncBar(pending: state.pending, lastSynced: state.lastSynced),
          Expanded(
            child: _loading
                ? const Center(child: CircularProgressIndicator())
                : _claims.isEmpty
                    ? const EmptyState(
                        icon: Icons.receipt_long_outlined,
                        title: 'No claims yet',
                        message: 'Fuel, refreshment, literature — claim it '
                            'here and the office approves it.',
                      )
                    : ListView.builder(
                        padding: const EdgeInsets.fromLTRB(12, 8, 12, 90),
                        itemCount: _claims.length,
                        itemBuilder: (context, index) {
                          final claim =
                              Map<String, dynamic>.from(_claims[index]);
                          final status = '${claim['status']}';

                          return Card(
                            child: ListTile(
                              title: Text('${claim['category_name']}'),
                              subtitle: Text(
                                '${claim['date']} · ${claim['description'] ?? ''}',
                                style: const TextStyle(fontSize: 12.5),
                              ),
                              trailing: Column(
                                mainAxisAlignment: MainAxisAlignment.center,
                                crossAxisAlignment: CrossAxisAlignment.end,
                                children: [
                                  Text(
                                    money.format(
                                      double.tryParse('${claim['amount']}') ?? 0,
                                    ),
                                    style: const TextStyle(
                                      fontWeight: FontWeight.bold,
                                    ),
                                  ),
                                  const SizedBox(height: 3),
                                  Pill(
                                    status,
                                    color: status == 'approved'
                                        ? brandGreen
                                        : status == 'rejected'
                                            ? const Color(0xFFDC3545)
                                            : const Color(0xFF8A6100),
                                  ),
                                ],
                              ),
                            ),
                          );
                        },
                      ),
          ),
        ],
      ),
      floatingActionButton: FloatingActionButton.extended(
        onPressed: () => _claim(context),
        icon: const Icon(Icons.add),
        label: const Text('Claim'),
      ),
    );
  }

  Future<void> _claim(BuildContext context) async {
    final amount = TextEditingController();
    final description = TextEditingController();
    ExpenseCategory? category;
    var date = DateTime.now();

    await showModalBottomSheet<void>(
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
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              const Text(
                'New Claim',
                style: TextStyle(
                  fontSize: 17,
                  fontWeight: FontWeight.w700,
                  color: brandDark,
                ),
              ),
              const SizedBox(height: 18),
              DropdownButtonFormField<ExpenseCategory>(
                initialValue: category,
                isExpanded: true,
                hint: const Text('What is it for?'),
                items: [
                  for (final option in context.read<AppState>().expenseCategories)
                    DropdownMenuItem(value: option, child: Text(option.name)),
                ],
                onChanged: (value) => setSheetState(() => category = value),
              ),
              const SizedBox(height: 12),
              TextField(
                controller: amount,
                keyboardType: const TextInputType.numberWithOptions(
                  decimal: true,
                ),
                decoration: const InputDecoration(labelText: 'Amount'),
              ),
              const SizedBox(height: 12),
              TextField(
                controller: description,
                decoration: const InputDecoration(labelText: 'Description'),
              ),
              const SizedBox(height: 12),
              OutlinedButton.icon(
                style: OutlinedButton.styleFrom(
                  minimumSize: const Size.fromHeight(50),
                  alignment: Alignment.centerLeft,
                ),
                icon: const Icon(Icons.calendar_today_outlined, size: 18),
                label: Text(fullDate.format(date)),
                onPressed: () async {
                  final picked = await showDatePicker(
                    context: sheetContext,
                    initialDate: date,
                    firstDate:
                        DateTime.now().subtract(const Duration(days: 60)),
                    lastDate: DateTime.now(),
                  );

                  if (picked != null) setSheetState(() => date = picked);
                },
              ),
              const SizedBox(height: 22),
              FilledButton(
                onPressed: () async {
                  final value = double.tryParse(amount.text.trim()) ?? 0;

                  if (category == null || value <= 0) {
                    showMessage(
                      sheetContext,
                      'Pick a category and enter an amount.',
                      bad: true,
                    );

                    return;
                  }

                  await sheetContext.read<AppState>().claimExpense(
                        categoryId: category!.id,
                        amount: value,
                        date: date,
                        description: description.text.trim(),
                      );

                  if (!sheetContext.mounted) return;

                  Navigator.of(sheetContext).pop();
                },
                child: const Text('Submit Claim'),
              ),
              const SizedBox(height: 8),
              const Text(
                'Claims start as pending until the office approves them.',
                textAlign: TextAlign.center,
                style: TextStyle(fontSize: 12, color: muted),
              ),
            ],
          ),
        ),
      ),
    );

    await _load();
  }
}
