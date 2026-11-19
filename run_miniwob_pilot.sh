#!/usr/bin/env bash
# Run from any directory. Results and the virtual environment live next to hybrid_agent/.
set -euo pipefail

cd -- "$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
python_bin=".venv-miniwob/bin/python"
config="hybrid_agent/config.yaml"
output_dir="${MINIWOB_OUTPUT_DIR:-runs/miniwob-pilot}"

die() { printf '%s\n' "$*" >&2; exit 2; }

# Only system packages use sudo. pip and all episodes run as the invoking user.
install_apt_packages() {
    command -v apt-get >/dev/null || die "请先安装系统依赖：$*，然后重新运行脚本。"
    local -a elevate=()
    if (( EUID != 0 )); then
        command -v sudo >/dev/null || die "请管理员安装系统依赖：$*，然后重新运行脚本。"
        elevate=(sudo)
    fi
    "${elevate[@]}" apt-get update
    "${elevate[@]}" apt-get install -y "$@"
}

[[ -r "$config" ]] || die "找不到 $PWD/$config，请完整解压运行包。"
printf '准备 Python 环境\n'
if [[ ! -x "$python_bin" ]] || ! "$python_bin" -m pip --version >/dev/null 2>&1; then
    if ! python3 -c 'import ensurepip, venv' >/dev/null 2>&1; then
        install_apt_packages python3 python3-venv
    fi
    python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' ||
        die "请让 python3 指向 Python 3.10 或更高版本，然后重新运行。"
    python3 -m venv .venv-miniwob
fi
"$python_bin" -m pip --disable-pip-version-check install -r hybrid_agent/requirements-miniwob.txt

if [[ -n "${MINIWOB_CHROME_BINARY:-}${MINIWOB_CHROMEDRIVER:-}" ]]; then
    [[ -f "${MINIWOB_CHROME_BINARY:-}" && -x "${MINIWOB_CHROME_BINARY:-}" &&
       -f "${MINIWOB_CHROMEDRIVER:-}" && -x "${MINIWOB_CHROMEDRIVER:-}" ]] ||
        die "请将 MINIWOB_CHROME_BINARY 和 MINIWOB_CHROMEDRIVER 同时指向可执行文件，或同时取消设置以自动检测。"
else
    browser=$(command -v chromium || command -v google-chrome || command -v chromium-browser || command -v google-chrome-stable || true)
    driver=$(command -v chromedriver || command -v chromium.chromedriver || true)
    distro=$(if [[ -r /etc/os-release ]]; then . /etc/os-release; printf '%s' "${ID:-}"; fi)
    if [[ "$distro" == debian && ( -z "$driver" || -z "$browser" ) ]] &&
       { (( EUID == 0 )) || command -v sudo >/dev/null; }; then
        install_apt_packages chromium chromium-driver
        browser=$(command -v chromium)
        driver=$(command -v chromedriver)
    fi
    if [[ -n "$browser" && -n "$driver" ]]; then
        export MINIWOB_CHROME_BINARY="$browser" MINIWOB_CHROMEDRIVER="$driver"
    else
        printf '将由 Selenium Manager 检测或下载 Chrome 和 ChromeDriver。\n'
    fi
fi

runner=("$python_bin" -m hybrid_agent.adapters.run_miniwob)
printf '\n开始浏览器自检（无需模型）\n'
"${runner[@]}" --smoke-test
printf '\n自检通过。开始 3 个任务 × 5 个 seed，使用 %s\n汇总文件：%s/summary.json\n' "$config" "$output_dir"
exec "${runner[@]}" --config "$config" \
    --tasks click-test-2 enter-text click-checkboxes --seeds 0 1 2 3 4 \
    --output-dir "$output_dir" --resume
