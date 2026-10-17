"""M1 paywalled hello endpoint using x402 seller middleware on Base Sepolia."""

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from x402 import x402ResourceServer
from x402.http import FacilitatorConfig, HTTPFacilitatorClient, PaymentOption
from x402.http.middleware.fastapi import PaymentMiddlewareASGI
from x402.http.types import RouteConfig
from x402.mechanisms.evm.exact import ExactEvmServerScheme

from config import settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.x402_server = x402_server
    yield


app = FastAPI(title="x402 Warranty M1 Hello", lifespan=lifespan)

facilitator = HTTPFacilitatorClient(FacilitatorConfig(url=settings.facilitator_url))
x402_server = x402ResourceServer(facilitator)
x402_server.register("eip155:84532", ExactEvmServerScheme())

routes = {
    "GET /hello": RouteConfig(
        accepts=[
            PaymentOption(
                scheme="exact",
                price="$0.001",
                network="eip155:84532",
                pay_to=settings.treasury_address,
            )
        ],
        description="Paywalled hello world for M1 verification",
        mime_type="application/json",
    )
}

app.add_middleware(PaymentMiddlewareASGI, routes=routes, server=x402_server)


@app.get("/hello")
async def hello(request: Request):
    payload = request.state.payment_payload
    payer = ""
    if payload and payload.payload:
        raw = payload.payload
        if "authorization" in raw:
            payer = raw["authorization"].get("from", "")
        elif "permit2Authorization" in raw:
            payer = raw["permit2Authorization"].get("from", "")
    return JSONResponse({"message": "hello x402", "payer": payer, "network": settings.x402_network})


@app.get("/health")
async def health():
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=settings.host, port=settings.port)
