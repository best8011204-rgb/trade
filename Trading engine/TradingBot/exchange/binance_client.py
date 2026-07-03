from __future__ import annotations

from uuid import uuid4

from TradingBot.exchange.models import OrderRequest, OrderResult, OrderStatus, SymbolRules
from TradingBot.exchange.rest import BinanceRestClient


def _map_order_status(status: object) -> OrderStatus:
    exchange_status = str(status)
    if exchange_status == "NEW":
        return OrderStatus.ACCEPTED
    return OrderStatus(exchange_status)


class BinanceClient:
    """Exchange facade hiding Binance-specific REST details from the engine."""

    def __init__(self, rest: BinanceRestClient) -> None:
        self._rest = rest

    async def close(self) -> None:
        await self._rest.close()

    async def sync_time(self) -> None:
        await self._rest.sync_time()

    async def get_symbol_rules(self, symbol: str) -> SymbolRules:
        info = await self._rest.request("GET", "/fapi/v1/exchangeInfo")
        symbols = info["symbols"] if isinstance(info, dict) else []
        for item in symbols:
            if item["symbol"] == symbol:
                lot_filter = next(f for f in item["filters"] if f["filterType"] == "LOT_SIZE")
                price_filter = next(f for f in item["filters"] if f["filterType"] == "PRICE_FILTER")
                return SymbolRules(
                    symbol=symbol,
                    tick_size=price_filter["tickSize"],
                    lot_size=lot_filter["stepSize"],
                    min_quantity=float(lot_filter["minQty"]),
                    price_precision=int(item["pricePrecision"]),
                    quantity_precision=int(item["quantityPrecision"]),
                )
        raise ValueError(f"Unknown symbol: {symbol}")

    async def submit_order(self, request: OrderRequest) -> OrderResult:
        client_order_id = request.client_order_id or f"engine-{uuid4().hex[:24]}"
        params: dict[str, object] = {
            "symbol": request.symbol,
            "side": request.side.value,
            "type": request.order_type.value,
            "quantity": request.quantity,
            "newClientOrderId": client_order_id,
            "reduceOnly": "true" if request.reduce_only else "false",
        }
        if request.price is not None:
            params["price"] = request.price
            params["timeInForce"] = "GTX" if request.post_only else "GTC"
        if request.stop_price is not None:
            params["stopPrice"] = request.stop_price

        raw = await self._rest.request("POST", "/fapi/v1/order", params=params, signed=True)
        assert isinstance(raw, dict)
        return OrderResult(
            exchange_order_id=str(raw.get("orderId", "")),
            client_order_id=str(raw.get("clientOrderId", client_order_id)),
            status=_map_order_status(raw.get("status", OrderStatus.SUBMITTED.value)),
            filled_quantity=float(raw.get("executedQty", 0)),
            average_price=float(raw["avgPrice"]) if raw.get("avgPrice") else None,
            raw=raw,
        )

    async def cancel_order(self, symbol: str, order_id: str) -> dict[str, object]:
        raw = await self._rest.request(
            "DELETE", "/fapi/v1/order", params={"symbol": symbol, "orderId": order_id}, signed=True
        )
        assert isinstance(raw, dict)
        return raw

    async def cancel_all(self, symbol: str) -> dict[str, object]:
        raw = await self._rest.request(
            "DELETE", "/fapi/v1/allOpenOrders", params={"symbol": symbol}, signed=True
        )
        assert isinstance(raw, dict)
        return raw
