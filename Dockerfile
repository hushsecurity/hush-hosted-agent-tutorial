# Packages are installed here, with the same Python (3.13) and C library
# (Debian 13) as the image they run in.
FROM python:3.13-slim-trixie AS build

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --no-compile --root-user-action=ignore --target /app/packages -r /tmp/requirements.txt

COPY agent /app/agent
COPY expenses /app/expenses
COPY setup /app/setup
# Compiled now, as nothing may be written at run time. Writable by no one, even
# on platforms that run pods in the root group.
RUN python -m compileall -q /app && chmod -R go-w /app

# No shell, no package manager: only Python and the code.
FROM gcr.io/distroless/python3-debian13:nonroot

COPY --from=build /app /app

WORKDIR /app
ENV PYTHONPATH=/app/packages \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# One image, three programs: `agent` (the default), `expenses` or `setup`.
ENTRYPOINT ["/usr/bin/python3.13", "-m"]
CMD ["agent"]
