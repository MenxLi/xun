FROM ubuntu:noble

RUN sed -i \
    -e 's|http://archive.ubuntu.com/ubuntu/|http://mirrors.aliyun.com/ubuntu/|g' \
    -e 's|http://security.ubuntu.com/ubuntu/|http://mirrors.aliyun.com/ubuntu/|g' \
    /etc/apt/sources.list.d/ubuntu.sources \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean \
    && apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && sed -i 's|http://mirrors.aliyun.com/ubuntu/|https://mirrors.aliyun.com/ubuntu/|g' \
        /etc/apt/sources.list.d/ubuntu.sources \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get update

    # && apt-get upgrade -y

# some dev tools
RUN apt-get install -y \
    git \
    cmake \
    ninja-build \
    build-essential

# install other dependencies and common utilities
RUN apt-get install -y apt-utils
RUN apt-get install -y rsync openssh-server tmux vim neovim
RUN apt-get install -y curl wget iproute2 iputils-ping proxychains-ng

# === cpp things ===
RUN apt-get install -y clangd bear

# === python things ===
RUN apt-get install -y python3 python3-pip python3-dev python3-venv
RUN pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple
RUN echo '\nalias python=python3\nalias pip=pip3\n' >> .bashrc

# === node ===
# https://www.nvmnode.com/zh/guide/download.html
RUN curl -o- https://cdn.gh-proxy.org/raw.githubusercontent.com/nvm-sh/nvm/v0.40.3/install.sh | bash
ENV NVM_DIR="/root/.nvm"
RUN bash -c "source $NVM_DIR/nvm.sh && nvm install --lts && nvm use --lts"

# === rust ===
ENV RUSTUP_DIST_SERVER=https://mirrors.tuna.tsinghua.edu.cn/rustup
ENV RUSTUP_UPDATE_ROOT=https://mirrors.tuna.tsinghua.edu.cn/rustup/rustup
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y

# common libs
RUN apt-get install -y libsqlite3-dev libomp-dev libnuma-dev

# === system utilities ===
RUN apt-get install -y htop iftop lsof
RUN apt-get install -y zip unzip ripgrep
RUN apt-get install -y --no-install-recommends \
    gnupg jq less tree file \
    fd-find git-lfs \
    procps psmisc netcat-openbsd dnsutils \
    shellcheck \
    gdb clang-format pkg-config \
    tini

# locale
RUN apt-get install -y locales
RUN locale-gen zh_CN.UTF-8
ENV LANG=zh_CN.UTF-8
ENV LANGUAGE=zh_CN:zh
ENV LC_ALL=zh_CN.UTF-8

# timezone
ARG TZ=Asia/Shanghai
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone
RUN DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends tzdata
RUN dpkg-reconfigure --frontend noninteractive tzdata

# applications (office)
RUN apt-get install -y \
    libreoffice-impress libreoffice-writer libreoffice-calc libreoffice-math \
    fonts-noto-cjk
RUN apt-get install -y evince poppler-utils
RUN apt-get install -y ghostscript imagemagick
RUN apt-get install -y python3-uno

# create default venv
ENV VIRTUAL_ENV=/venv
RUN python3 -m venv $VIRTUAL_ENV
ENV PATH="$VIRTUAL_ENV/bin:$PATH"
RUN pip install --upgrade pip setuptools wheel

# install deps ahead of time to leverage Docker layer caching
RUN pip install playwright
RUN playwright install chromium --with-deps

# install pip managed applications
RUN pip install ffmpeg-python opencv-python-headless
RUN pip install numpy scipy pandas matplotlib pymupdf

# === install xun ===
# my extra deps
RUN pip install pyyaml httpx[socks] 
RUN pip install zai-sdk

COPY . /xun-src
WORKDIR /xun-src
RUN pip install .

ENV _XUN_INFO_TOOL_OVERRIDE=1

# fixed xun home: xunc/xunx copy the host home in here at container creation (follows symlinks)
ENV XUN_HOME=/.xun

RUN mkdir -p /workspace /.xun
WORKDIR /workspace
