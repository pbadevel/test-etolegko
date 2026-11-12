# speedtest-cli

Sequential HTTP download speed tester on Python + Pydantic v2.

Makes N **sequential** requests to a URL, waits for the full response body each time,
then computes the average request time, total bytes downloaded, and average speed.

## Units

- **Speed** — reported in **Mbps** (megabits per second, decimal `10^6 bit/s`) —
  the standard unit ISPs and speedtests use. In parentheses also shown as
  **MB/s** (megabytes per second).
- **Volume** — bytes / KB / MB / GB, powers of 1024.

## Requirements

I'm using **uv** packet manager. Install it with:

```bash
pipx install uv
```
or

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```
Install requiremets:
```bash
uv sync
```

## Usage

```bash
uv run speedtest.py <URL> [-n 10] [-t 60] [-c 1] [--json]
```

-n, --runs — total number of requests (default: 10)

-t, --timeout — per-request timeout, seconds (default: 60)

-c, --connections — max concurrent connections (default: 1 = sequential)

--json — machine-readable output (full Pydantic dump)
