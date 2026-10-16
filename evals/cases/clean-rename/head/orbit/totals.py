def line_total(item):
    return item["price"] * item["qty"]


def total(items):
    amount = 0
    for item in items:
        amount += line_total(item)
    return amount


def total_with_tax(items, rate):
    amount = total(items)
    return round(amount * (1 + rate), 2)
