# Tally Bot — stdlib-only, no pip install needed
FROM python:3.12-slim

# tzdata lets zoneinfo resolve Asia/Yangon; without it the code falls
# back to a fixed +06:30 offset, which is also correct for Myanmar.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && ln -sf /usr/share/zoneinfo/Asia/Yangon /etc/localtime \
    && echo "Asia/Yangon" > /etc/timezone

# Unbuffered output so `fly logs` shows lines immediately.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY main.py tally.py ./
COPY src ./src
COPY tests ./tests

RUN mkdir -p state

CMD ["python3", "main.py", "--run"]
