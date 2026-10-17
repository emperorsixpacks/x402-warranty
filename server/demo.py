"""Flaky demo upstream for testing."""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from x402 import x402ResourceServer
from x402.http import FacilitatorConfig, HTTPFacilitatorClient, PaymentOption
from x402.http.middleware.fastapi import PaymentMiddlewareASGI
from x402.http.types import RouteConfig
from x402.mechanisms.evm.exact import ExactEvmServerScheme

from config import settings


facilitator = HTTPFacilitatorClient(FacilitatorConfig(url=settings.facilitator_url))
x402_server = x402ResourceServer(facilitator)
x402_server.register(settings.x402_network, ExactEvmServerScheme())

demo_app = FastAPI()

routes = {
    "GET /data": RouteConfig(
        accepts=[
            PaymentOption(
                scheme="exact",
                price="$0.01",
                network=settings.x402_network,
                pay_to=settings.treasury_address,
            )
        ],
        description="Demo upstream data",
        mime_type="application/json",
    )
}

demo_app.add_middleware(PaymentMiddlewareASGI, routes=routes, server=x402_server)


@demo_app.get("/data")
async def demo_data(request: Request):
    """Demo upstream with objective failure modes for v3 refunds.

    Modes (query params):
      fail=500     -> HTTP 500 (refund: non_2xx)
      fail=empty   -> HTTP 200 with empty body (refund: empty_body)
      fail=badjson -> HTTP 200, JSON content-type, malformed body (refund: invalid_json)
      junk=1       -> HTTP 200 valid JSON junk (v3: PASSES — content is not a refund event)
      junk=flip    -> flaky version of the above
    """
    query = request.query_params
    fail = query.get("fail")
    if fail == "500":
        return JSONResponse({"error": "upstream exploded"}, status_code=500)
    if fail == "empty":
        return Response(content="", media_type="application/json")
    if fail == "badjson":
        return Response(content='{"broken": tru', media_type="application/json")
    junk = query.get("junk")
    if junk == "1":
        return JSONResponse({"garbage": "this is not useful data", "status": "junk"})
    if junk == "flip":
        import random
        if random.random() < 0.5:
            return JSONResponse({"garbage": "flaky junk", "status": "junk"})
    return JSONResponse({"message": "hello warranty", "status": "good", "data": {"value": 42}})
