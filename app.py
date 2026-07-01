import os

from app import create_app


app = create_app()
application = app

print(f"Running on PORT: {os.environ.get('PORT', '5000')}", flush=True)
print(f"Database path: {app.config['DATABASE']}", flush=True)
print(
    f"Environment mode: {os.environ.get('LABOUR_OS_ENV', 'production')}",
    flush=True,
)


if __name__ == "__main__":
    # Local fallback only. Render starts the application with Gunicorn.
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
