import asyncio
from decimal import Decimal, getcontext
import logging
import traceback

# from asyncio import CancelledError
from datetime import timezone


from lumibot.data_sources import CcxtData
from lumibot.entities import Asset, Order, Position

from .broker import Broker


class Ccxt(CcxtData, Broker):
    """Inherit CcxtData first and all the price market
    methods than inherits broker

    """

    def __init__(self, config, max_workers=20, chunk_size=100, connect_stream=False):
        # Calling init methods
        CcxtData.__init__(self, config, max_workers=max_workers, chunk_size=chunk_size)
        Broker.__init__(self, name="ccxt", connect_stream=connect_stream)

        self.market = "24/7"

    # =========Clock functions=====================

    def get_timestamp(self):
        """Returns the current UNIX timestamp representation from CCXT.

        Parameters
        ----------
        None
        """
        logging.warning(
            "The method 'get_time_to_close' is not applicable with Crypto 24/7 markets."
        )
        return self.api.microseconds() / 1000000

    def is_market_open(self):
        """Not applicable with Crypto 24/7 markets.

        Returns
        -------
        None
        """
        logging.warning(
            "The method 'is_market_open' is not applicable with Crypto 24/7 markets."
        )
        return None

    def get_time_to_open(self):
        """Not applicable with Crypto 24/7 markets.

        Returns
        -------
        None
        """
        logging.warning(
            "The method 'get_time_to_open' is not applicable with Crypto 24/7 markets."
        )
        return None

    def get_time_to_close(self):
        """Not applicable with Crypto 24/7 markets.

        Returns
        -------
        None
        """
        logging.warning(
            "The method 'get_time_to_close' is not applicable with Crypto 24/7 markets."
        )
        return None

    # =========Positions functions==================
    def _get_balances_at_broker(self):
        """Get's the current actual cash, positions value, and total
        liquidation value from Alpaca.

        This method will get the current actual values from Alpaca
        for the actual cash, positions value, and total liquidation.

        Returns
        -------
        tuple of float
            (cash, positions_value, total_liquidation_value)
        """
        base_currency = "USD"
        total_cash_value = 0
        positions_value = 0
        # Get the market values for each pair held.
        balances = self.api.fetch_balance()
        for currency_info in balances["info"]:
            currency = currency_info["currency"]
            # if currency != 'BTC':
            #     continue
            market = f"{currency}/{base_currency}"
            try:
                assert market in self.api.markets
            except AssertionError:
                logging.error(f"Market {market} not found in ccxt.markets")
                continue
            precision_amount = self.api.markets[market]["precision"]["amount"]
            precision_price = self.api.markets[market]["precision"]["price"]
            units = Decimal(currency_info["balance"]).quantize(
                Decimal(str(precision_amount))
            )
            price = Decimal(self.api.fetch_ticker(market)["last"]).quantize(
                Decimal(str(precision_price))
            )
            value = units * price
            positions_value += value

        gross_positions_value = float(positions_value)
        net_liquidation_value = float(positions_value)

        return (total_cash_value, gross_positions_value, net_liquidation_value)

    def _parse_broker_position(self, position, strategy, orders=None):
        """parse a broker position representation
        into a position object"""
        asset = Asset(
            symbol=position["currency"],
            asset_type="crypto",
            precision=str(self.api.currencies["BTC"]["precision"]),
        )
        quantity = Decimal(position["balance"])
        hold = position["hold"]
        available = position["available"]

        position = Position(
            strategy, asset, quantity, hold=hold, available=available, orders=orders
        )
        return position

    def _pull_broker_position(self, asset):
        """Given a asset, get the broker representation
        of the corresponding asset"""
        response = self._pull_broker_positions()["info"][asset.symbol]
        return response

    def _pull_broker_positions(self):
        """Get the broker representation of all positions"""
        response = self.api.fetch_balance()
        return response["info"]

    # =======Orders and assets functions=========
    def _parse_broker_order(self, response, strategy):
        """parse a broker order representation
        to an order object"""
        pair = response["symbol"].split("/")
        order = Order(
            strategy,
            Asset(
                symbol=pair[0],
                asset_type="crypto",
            ),
            response["amount"],
            response["side"],
            limit_price=response["price"],
            stop_price=response["stopPrice"],
            time_in_force=response["timeInForce"].lower(),
            quote=Asset(
                symbol=pair[1],
                asset_type="crypto",
            ),
        )
        order.set_identifier(response["id"])
        order.update_status(response["status"])
        order.update_raw(response)
        return order

    def _pull_broker_order(self, id):
        """Get a broker order representation by its id"""
        open_orders = self._pull_broker_open_orders()
        closed_orders = self.api.fetch_closed_orders()
        all_orders = open_orders + closed_orders

        response = [order for order in all_orders if order["id"] == id]

        return response[0] if len(response) > 0 else None

    def _pull_broker_open_orders(self):
        """Get the broker open orders"""
        orders = self.api.fetch_open_orders()
        return orders

    def _flatten_order(self, order):
        """Some submitted orders may trigger other orders.
        _flatten_order returns a list containing the main order
        and all the derived ones"""
        orders = [order]
        if "legs" in order._raw and order._raw.legs:
            strategy = order.strategy
            for json_sub_order in order._raw.legs:
                sub_order = self._parse_broker_order(json_sub_order, strategy)
                orders.append(sub_order)

        return orders

    def _submit_order(self, order):
        """Submit an order for an asset"""

        # Check order within limits.
        market = self.api.markets.get(order.pair, None)
        if market is None:
            logging.error(
                f"An order for {order.pair} was submitted. The market for that pair does not exist"
            )
            order.set_error("No market for pair.")
            return order

        limits = market["limits"]
        precision = market["precision"]

        # Convert the amount to Decimal.
        if hasattr(order, "quantity") and getattr(order, "quantity") is not None:
            setattr(
                order,
                "quantity",
                Decimal(getattr(order, "quantity")).quantize(
                    Decimal(str(precision["amount"]))
                ),
            )
            try:
                if limits["amount"]["min"] is not None:
                    assert order.quantity >= limits["amount"]["min"]
            except AssertionError:
                logging.warning(
                    f"\nThe order {order} was rejected as the order quantity \n"
                    f"was less then the minimum allowed for {order.pair}. The minimum order quantity is {limits['amount']['min']} \n"
                    f"The quantity for this order was {order.quantity} \n"
                )
                return

            try:
                if limits["amount"]["max"] is not None:
                    assert order.quantity <= limits["amount"]["max"]
            except AssertionError:
                logging.warning(
                    f"\nThe order {order} was rejected as the order quantity \n"
                    f"was greater then the maximum allowed for {order.pair}. The maximum order "
                    f"quantity is {limits['amount']['max']} \n"
                    f"The quantity for this order was {order.quantity} \n"
                )
                return

        # Convert the price to Decimal.
        for price_type in [
            "limit_price",
            "stop_price",
        ]:
            if hasattr(order, price_type) and getattr(order, price_type) is not None:
                setattr(
                    order,
                    price_type,
                    Decimal(getattr(order, price_type)).quantize(
                        Decimal(str(precision["price"]))
                    ),
                )
            else:
                continue

            try:
                if limits["price"]["min"] is not None:
                    assert getattr(order, price_type) >= limits["price"]["min"]
            except AssertionError:
                logging.warning(
                    f"\nThe order {order} was rejected as the order {price_type} \n"
                    f"was less then the minimum allowed for {order.pair}. The minimum price "
                    f"is {limits['price']['min']} \n"
                    f"The price for this order was {getattr(order, price_type):4.9f} \n"
                )
                return

            try:
                if limits["price"]["max"] is not None:
                    assert getattr(order, price_type) <= limits["price"]["max"]
            except AssertionError:
                logging.warning(
                    f"\nThe order {order} was rejected as the order {price_type} \n"
                    f"was greater then the maximum allowed for {order.pair}. The maximum price "
                    f"is {limits['price']['max']} \n"
                    f"The price for this order was {getattr(order, price_type):4.9f} \n"
                )
                return

            try:
                if limits["cost"]["min"] is not None:
                    assert (
                        getattr(order, price_type) * order.quantity
                        >= limits["cost"]["min"]
                    )
            except AssertionError:
                logging.warning(
                    f"\nThe order {order} was rejected as the order total cost \n"
                    f"was less then the minimum allowed for {order.pair}. The minimum cost "
                    f"is {limits['cost']['min'] * order.quantity} \n"
                    f"The cost for this order was "
                    f"{(getattr(order, price_type) * order.quantity):4.9f} \n"
                )
                return

            try:
                if limits["cost"]["max"] is not None:
                    assert (
                        getattr(order, price_type) * order.quantity
                        <= limits["cost"]["max"]
                    )
            except AssertionError:
                logging.warning(
                    f"\nThe order {order} was rejected as the order total cost \n"
                    f"was greater then the maximum allowed for {order.pair}. The maximum cost "
                    f"is {limits['cost']['max'] * order.quantity} \n"
                    f"The cost for this order was "
                    f"{(getattr(order, price_type) * order.quantity):4.9f} \n"
                )
                return

        params = {}
        # Current custom params are for Coinbase only.
        if order.type in ["stop", "stop_limit"]:
            params = {
                "stop": "entry" if order.side == "buy" else "loss",
                "stop_price": order.stop_price,
            }
            # Remove items with None values
            params = {k: v for k, v in params.items() if v}

        order_type_map = dict(
            market="market",
            stop="market",
            limit="limit",
            stop_limit="limit",
        )

        args = [
            order.pair,
            order_type_map[order.type],
            order.side,
            order.quantity,
        ]
        if order_type_map[order.type] == "limit":
            args.append(order.limit_price)

        args.append(params)

        try:
            response = self.api.create_order(*args)
            order.set_identifier(response["id"])
            order.update_status(response["status"])
            order.update_raw(response)

        except Exception as e:
            order.set_error(e)
            message = str(e)
            logging.info(
                "%r did not go through. The following error occurred: %s"
                % (order, message)
            )

        return order

    def cancel_order(self, order):
        """Cancel an order"""
        response = self.api.cancel_order(order.identifier)
        if order.identifier == response:
            order.set_canceled()