from django.contrib import admin
from .models import Customer, Invoice, Item,UserRolls, InvoiceLog


class ItemInline(admin.TabularInline):
    model = Item
    extra = 0


class InvoiceAdmin(admin.ModelAdmin):
    list_display = ('invoice_no', 'customer', 'date', 'license_no')
    inlines = [ItemInline]

@admin.register(UserRolls)
class UserRollAdmin(admin.ModelAdmin):
    list_display = ('user', 'role')
    list_filter = ('role',)
    
@admin.register(InvoiceLog)
class InvoiceLogAdmin(admin.ModelAdmin):
    # Admin panel mein ye columns nazar ayenge
    list_display = ('id', 'user', 'invoice', 'customer_name', 'amount', 'timestamp')
    # Search karne ke liye fields
    search_fields = ('invoice__invoice_no', 'user__username', 'customer_name')
    # Side bar mein filters
    list_filter = ('timestamp', 'user')


admin.site.register(Customer)
admin.site.register(Invoice, InvoiceAdmin)
admin.site.register(Item)
