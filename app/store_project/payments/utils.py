def int_to_price(price: int) -> str:
    """Takes an int representing price in cents and returns a string
    representing a dollar amount with two decimal places."""
    return f"{float(price / 100):.2f}"
