from typing import NamedTuple, Optional
import heapq


class Fill(NamedTuple):
    buy_order_id: str
    sell_order_id: str
    price: int
    quantity: int


class OrderBook:
    def __init__(self):
        self.orders: dict[str, dict] = {}
        self.buy_levels: dict[int, dict[str, dict]] = {}
        self.sell_levels: dict[int, dict[str, dict]] = {}
        self.buy_heap: list[int] = []
        self.sell_heap: list[int] = []

    def _get_best_buy_price(self) -> Optional[int]:
        while self.buy_heap:
            p = -self.buy_heap[0]
            if p in self.buy_levels and self.buy_levels[p]:
                return p
            heapq.heappop(self.buy_heap)
        return None

    def _get_best_sell_price(self) -> Optional[int]:
        while self.sell_heap:
            p = self.sell_heap[0]
            if p in self.sell_levels and self.sell_levels[p]:
                return p
            heapq.heappop(self.sell_heap)
        return None

    def add_order(self, order_id: str, side: str, price: int, quantity: int) -> list[Fill]:
        order = {"side": side, "price": price, "qty": quantity}
        self.orders[order_id] = order
        fills: list[Fill] = []

        if side == "buy":
            while order["qty"] > 0:
                best_ask = self._get_best_sell_price()
                if best_ask is None or best_ask > price:
                    break
                resting_id = next(iter(self.sell_levels[best_ask]))
                resting = self.sell_levels[best_ask][resting_id]
                fill_qty = min(order["qty"], resting["qty"])
                fills.append(Fill(order_id, resting_id, best_ask, fill_qty))
                order["qty"] -= fill_qty
                resting["qty"] -= fill_qty
                if resting["qty"] == 0:
                    del self.sell_levels[best_ask][resting_id]
                    del self.orders[resting_id]
                    if not self.sell_levels[best_ask]:
                        del self.sell_levels[best_ask]
        else:
            while order["qty"] > 0:
                best_bid = self._get_best_buy_price()
                if best_bid is None or best_bid < price:
                    break
                resting_id = next(iter(self.buy_levels[best_bid]))
                resting = self.buy_levels[best_bid][resting_id]
                fill_qty = min(order["qty"], resting["qty"])
                fills.append(Fill(resting_id, order_id, best_bid, fill_qty))
                order["qty"] -= fill_qty
                resting["qty"] -= fill_qty
                if resting["qty"] == 0:
                    del self.buy_levels[best_bid][resting_id]
                    del self.orders[resting_id]
                    if not self.buy_levels[best_bid]:
                        del self.buy_levels[best_bid]

        if order["qty"] > 0:
            if side == "buy":
                levels, heap, key = self.buy_levels, self.buy_heap, -price
            else:
                levels, heap, key = self.sell_levels, self.sell_heap, price
            if price not in levels:
                levels[price] = {}
                heapq.heappush(heap, key)
            levels[price][order_id] = order

        return fills

    def cancel_order(self, order_id: str) -> bool:
        if order_id not in self.orders:
            return False
        order = self.orders[order_id]
        price = order["price"]
        side = order["side"]
        levels = self.buy_levels if side == "buy" else self.sell_levels
        if price in levels and order_id in levels[price]:
            del levels[price][order_id]
            if not levels[price]:
                del levels[price]
        del self.orders[order_id]
        return True

    def best_bid_ask(self) -> tuple[Optional[int], Optional[int]]:
        return (self._get_best_buy_price(), self._get_best_sell_price())
