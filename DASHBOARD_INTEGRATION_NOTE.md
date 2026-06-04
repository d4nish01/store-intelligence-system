# Dashboard Integration Note

AI-2's `app/main.py` may already include a placeholder `GET /dashboard` endpoint. To use the full dashboard in `app/dashboard.py`, update `app/main.py` during final merge:

1. Add this import near the other imports:

```python
from app.dashboard import router as dashboard_router
```

2. Add this after `app = FastAPI(...)`:

```python
app.include_router(dashboard_router)
```

3. Remove or comment out the placeholder `@app.get("/dashboard", response_class=HTMLResponse)` function from AI-2, otherwise there may be duplicate dashboard routes.

Do not change any event schema, backend endpoint names, or database models.
