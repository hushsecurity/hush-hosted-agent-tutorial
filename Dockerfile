# Packages are installed here, with the same Python (3.11) and C library
# (Debian 12) as the image they run in.
FROM python:3.11-slim-bookworm@sha256:a83ba18aef0f865b1a4a611c2cf460fe446f2795673bbb9fe43be570439e3b39 AS build

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --no-compile --root-user-action=ignore --target /app/packages -r /tmp/requirements.txt

COPY agent /app/agent
COPY expenses /app/expenses
# Compiled now, as nothing may be written at run time. Writable by no one, even
# on platforms that run pods in the root group.
RUN python -m compileall -q /app && chmod -R go-w /app

# No shell, no package manager: only Python and the code.
FROM gcr.io/distroless/python3-debian12:nonroot@sha256:7d1042ce588ab97019fe95c24ffca7bc5a82ccdac572511d5e09bda4435c89c5

COPY --from=build /app /app

WORKDIR /app
ENV PYTHONPATH=/app/packages \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# One image, two programs: `agent` (the default) or `expenses`.
ENTRYPOINT ["/usr/bin/python3.11", "-m"]
CMD ["agent"]
