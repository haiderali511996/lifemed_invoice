import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../models/models.dart';
import '../state/app_state.dart';
import '../theme.dart';
import '../widgets/common.dart';

/// Orders the MR has sent to the office.
///
/// An order is a request, not a sale: nothing is reserved and no price is
/// fixed until the office raises the invoice. The wording throughout says so,
/// because an MR who thinks stock is held will promise a delivery date.
class OrdersScreen extends StatefulWidget {
  const OrdersScreen({super.key});

  @override
  State<OrdersScreen> createState() => _OrdersScreenState();
}

class _OrdersScreenState extends State<OrdersScreen> {
  List<Order> _orders = [];
  bool _loading = true;

  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load() async {
    final orders = await context.read<AppState>().loadOrders();

    if (mounted) {
      setState(() {
        _orders = orders;
        _loading = false;
      });
    }
  }

  Color _colourFor(Order order) {
    if (order.pending) return const Color(0xFF8A6100);

    switch (order.status) {
      case 'invoiced':
        return brandGreen;
      case 'rejected':
      case 'cancelled':
        return const Color(0xFFDC3545);
      default:
        return brandBlue;
    }
  }

  @override
  Widget build(BuildContext context) {
    final state = context.watch<AppState>();

    return Scaffold(
      appBar: AppBar(title: const Text('My Orders')),
      body: Column(
        children: [
          SyncBar(pending: state.pending, lastSynced: state.lastSynced),
          Expanded(
            child: _loading
                ? const Center(child: CircularProgressIndicator())
                : _orders.isEmpty
                    ? const EmptyState(
                        icon: Icons.inventory_2_outlined,
                        title: 'No orders yet',
                        message: 'Take an order at a pharmacy and send it to '
                            'the office — they raise the invoice and deliver.',
                      )
                    : RefreshIndicator(
                        onRefresh: _load,
                        child: ListView.builder(
                          padding: const EdgeInsets.fromLTRB(12, 8, 12, 90),
                          itemCount: _orders.length,
                          itemBuilder: (context, index) =>
                              _OrderCard(
                            order: _orders[index],
                            colour: _colourFor(_orders[index]),
                            onCancel: _cancel,
                          ),
                        ),
                      ),
          ),
        ],
      ),
      floatingActionButton: FloatingActionButton.extended(
        onPressed: () async {
          await Navigator.of(context).push(
            MaterialPageRoute(builder: (_) => const PlaceOrderScreen()),
          );

          await _load();
        },
        icon: const Icon(Icons.add_shopping_cart),
        label: const Text('New Order'),
      ),
    );
  }

  Future<void> _cancel(Order order) async {
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (dialog) => AlertDialog(
        title: Text('Cancel ${order.orderNo}?'),
        content: const Text(
          'The office will see it withdrawn. You can place a new order after.',
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.of(dialog).pop(false),
            child: const Text('Keep it'),
          ),
          FilledButton(
            onPressed: () => Navigator.of(dialog).pop(true),
            child: const Text('Cancel order'),
          ),
        ],
      ),
    );

    if (confirmed != true || !mounted) return;

    final error = await context.read<AppState>().cancelOrder(order);

    if (!mounted) return;

    if (error != null) {
      showMessage(context, error, bad: true);
    } else {
      await _load();
    }
  }
}

class _OrderCard extends StatelessWidget {
  const _OrderCard({
    required this.order,
    required this.colour,
    required this.onCancel,
  });

  final Order order;
  final Color colour;
  final Future<void> Function(Order) onCancel;

  @override
  Widget build(BuildContext context) {
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(14),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                Expanded(
                  child: Text(
                    order.customerName,
                    style: const TextStyle(
                      fontWeight: FontWeight.w700,
                      fontSize: 15,
                    ),
                  ),
                ),
                Pill(order.pending ? 'not sent' : order.status, color: colour),
              ],
            ),
            const SizedBox(height: 3),
            Text(
              '${order.orderNo} · ${dayMonth.format(order.placedAt)}',
              style: const TextStyle(fontSize: 12, color: muted),
            ),

            if (order.lines.isNotEmpty) ...[
              const Divider(height: 18),
              ...order.lines.map(
                (line) => Padding(
                  padding: const EdgeInsets.only(bottom: 3),
                  child: Row(
                    children: [
                      Expanded(
                        child: Text(
                          line.productName,
                          style: const TextStyle(fontSize: 13),
                        ),
                      ),
                      Text(
                        '${line.qty} × ${money.format(line.unitPrice)}',
                        style: const TextStyle(fontSize: 12.5, color: muted),
                      ),
                    ],
                  ),
                ),
              ),
              const SizedBox(height: 8),
              Align(
                alignment: Alignment.centerRight,
                child: Text(
                  money.format(order.total),
                  style: const TextStyle(
                    fontWeight: FontWeight.bold,
                    fontSize: 15,
                  ),
                ),
              ),
            ],

            if (order.invoiceNo != null) ...[
              const SizedBox(height: 6),
              Text(
                'Invoiced as ${order.invoiceNo}',
                style: const TextStyle(fontSize: 12, color: brandGreen),
              ),
            ],

            if (order.pending) ...[
              const SizedBox(height: 6),
              Text(
                order.statusLabel,
                style: const TextStyle(fontSize: 12, color: Color(0xFF8A6100)),
              ),
            ],

            if (order.canCancel)
              Align(
                alignment: Alignment.centerLeft,
                child: TextButton(
                  onPressed: () => onCancel(order),
                  child: const Text('Cancel this order'),
                ),
              ),
          ],
        ),
      ),
    );
  }
}

// --------------------------------------------------------------- placing one

class PlaceOrderScreen extends StatefulWidget {
  const PlaceOrderScreen({super.key});

  @override
  State<PlaceOrderScreen> createState() => _PlaceOrderScreenState();
}

class _PlaceOrderScreenState extends State<PlaceOrderScreen> {
  final _customer = TextEditingController();
  final _address = TextEditingController();
  final _phone = TextEditingController();
  final _note = TextEditingController();

  CallPoint? _callPoint;
  DateTime? _requiredBy;
  final Map<int, int> _quantities = {};
  bool _saving = false;

  @override
  void dispose() {
    _customer.dispose();
    _address.dispose();
    _phone.dispose();
    _note.dispose();
    super.dispose();
  }

  List<OrderLine> get _lines {
    final products = context.read<AppState>().products;

    return [
      for (final product in products)
        if ((_quantities[product.id] ?? 0) > 0)
          OrderLine(
            productId: product.id,
            productName: product.name,
            qty: _quantities[product.id]!,
            // The office sets the real price on the invoice; sending zero
            // makes them use the product's trade price rather than guessing.
            unitPrice: 0,
            discount: 0,
          ),
    ];
  }

  Future<void> _send() async {
    final name = _customer.text.trim();

    if (name.isEmpty) {
      showMessage(context, 'Who is the order for?', bad: true);

      return;
    }

    if (_lines.isEmpty) {
      showMessage(context, 'Add a quantity against at least one product.',
          bad: true);

      return;
    }

    setState(() => _saving = true);

    await context.read<AppState>().placeOrder(
          customerName: name,
          callPointId: _callPoint?.id,
          lines: _lines,
          deliveryAddress: _address.text.trim(),
          contactNumber: _phone.text.trim(),
          note: _note.text.trim(),
          requiredBy: _requiredBy,
        );

    if (!mounted) return;

    Navigator.of(context).pop();
    showMessage(context, 'Order sent to the office.');
  }

  @override
  Widget build(BuildContext context) {
    final state = context.watch<AppState>();
    final total = _lines.fold<int>(0, (sum, line) => sum + line.qty);

    return Scaffold(
      appBar: AppBar(title: const Text('New Order')),
      body: ListView(
        padding: const EdgeInsets.all(16),
        children: [
          const _Heading('Who it is for'),
          TextField(
            controller: _customer,
            decoration: const InputDecoration(
              labelText: 'Pharmacy or hospital',
              hintText: 'The name to invoice',
            ),
          ),
          const SizedBox(height: 12),

          DropdownButtonFormField<CallPoint>(
            initialValue: _callPoint,
            isExpanded: true,
            hint: const Text('Deliver to a call point (optional)'),
            items: [
              for (final point in state.callPoints)
                DropdownMenuItem(value: point, child: Text(point.name)),
            ],
            onChanged: (value) => setState(() {
              _callPoint = value;

              // Saves retyping the two things most likely to already be known.
              if (value != null) {
                if (_customer.text.trim().isEmpty) _customer.text = value.name;
                if (_address.text.trim().isEmpty) _address.text = value.address;
                if (_phone.text.trim().isEmpty) _phone.text = value.phone;
              }
            }),
          ),
          const SizedBox(height: 12),

          TextField(
            controller: _address,
            decoration: const InputDecoration(labelText: 'Delivery address'),
          ),
          const SizedBox(height: 12),
          TextField(
            controller: _phone,
            keyboardType: TextInputType.phone,
            decoration: const InputDecoration(labelText: 'Contact number'),
          ),
          const SizedBox(height: 12),

          OutlinedButton.icon(
            style: OutlinedButton.styleFrom(
              minimumSize: const Size.fromHeight(50),
              alignment: Alignment.centerLeft,
            ),
            icon: const Icon(Icons.event_outlined, size: 18),
            label: Text(
              _requiredBy == null
                  ? 'Needed by — not stated'
                  : 'Needed by ${fullDate.format(_requiredBy!)}',
            ),
            onPressed: () async {
              final picked = await showDatePicker(
                context: context,
                initialDate: DateTime.now().add(const Duration(days: 2)),
                firstDate: DateTime.now(),
                lastDate: DateTime.now().add(const Duration(days: 120)),
              );

              if (picked != null) setState(() => _requiredBy = picked);
            },
          ),

          const SizedBox(height: 22),
          _Heading('What they want${total > 0 ? ' · $total units' : ''}'),

          if (state.products.isEmpty)
            const Text(
              'No products cached yet. Sync while you have signal.',
              style: TextStyle(color: muted, fontSize: 13),
            )
          else
            ...state.products.map(
              (product) => _ProductRow(
                name: product.name,
                qty: _quantities[product.id] ?? 0,
                onChanged: (value) => setState(
                  () => _quantities[product.id] = value,
                ),
              ),
            ),

          const SizedBox(height: 22),
          const _Heading('Note for the office'),
          TextField(
            controller: _note,
            maxLines: 2,
            decoration: const InputDecoration(
              hintText: 'Anything they should know before delivering',
            ),
          ),

          const SizedBox(height: 24),
          FilledButton.icon(
            onPressed: _saving ? null : _send,
            icon: const Icon(Icons.send),
            label: const Text('Send to Office'),
          ),
          const SizedBox(height: 10),
          const Text(
            'The office prices it and raises the invoice. Nothing is reserved '
            'until they do, so do not promise stock on this alone.',
            textAlign: TextAlign.center,
            style: TextStyle(fontSize: 12, color: muted),
          ),
        ],
      ),
    );
  }
}

class _ProductRow extends StatelessWidget {
  const _ProductRow({
    required this.name,
    required this.qty,
    required this.onChanged,
  });

  final String name;
  final int qty;
  final ValueChanged<int> onChanged;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.only(bottom: 6),
      child: Row(
        children: [
          Expanded(
            child: Text(
              name,
              style: TextStyle(
                fontSize: 14,
                fontWeight: qty > 0 ? FontWeight.w600 : FontWeight.normal,
                color: qty > 0 ? brandDark : Colors.black87,
              ),
            ),
          ),
          IconButton(
            visualDensity: VisualDensity.compact,
            icon: const Icon(Icons.remove_circle_outline),
            onPressed: qty > 0 ? () => onChanged(qty - 1) : null,
          ),
          SizedBox(
            width: 34,
            child: Text(
              '$qty',
              textAlign: TextAlign.center,
              style: const TextStyle(
                fontWeight: FontWeight.bold,
                fontSize: 15,
              ),
            ),
          ),
          IconButton(
            visualDensity: VisualDensity.compact,
            icon: const Icon(Icons.add_circle_outline),
            onPressed: () => onChanged(qty + 1),
          ),
        ],
      ),
    );
  }
}

class _Heading extends StatelessWidget {
  const _Heading(this.text);

  final String text;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.only(bottom: 10),
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
