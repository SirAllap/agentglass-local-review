def total(items):
    t = 0
    for i in items:
        t += i["price"] * i["qty"]
    return t


def total_with_tax(items, rate):
    t = total(items)
    return round(t * (1 + rate), 2)
