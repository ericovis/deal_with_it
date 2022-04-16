import os

from datetime import datetime
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from src.models import ImageModel, ApiResponseModel
from src.processors.deal_with_it import DealWithItProcessor


HERE = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(HERE, 'static')
TEMPLATES_DIR = os.path.join(HERE, 'templates')


app = FastAPI(redoc_url=None)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
templates = Jinja2Templates(directory=TEMPLATES_DIR)


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def index(request: Request):
    return templates.TemplateResponse("index.html", dict(request=request, now=datetime.now()))

@app.post("/api", response_model=ApiResponseModel)
async def api(image: ImageModel):
    proc = DealWithItProcessor(image, STATIC_DIR)
    proc.call()
    return dict(image=proc.base64_output)
