FROM ubuntu:24.04

RUN if [ -f /etc/apt/sources.list ]; then \
    sed -i \
    -e 's|http://archive.ubuntu.com/ubuntu/|http://mirrors.aliyun.com/ubuntu/|g' \
    -e 's|http://security.ubuntu.com/ubuntu/|http://mirrors.aliyun.com/ubuntu/|g' \
    /etc/apt/sources.list; \
    fi && \
    if [ -f /etc/apt/sources.list.d/ubuntu.sources ]; then \
    sed -i 's|http://archive.ubuntu.com/ubuntu/|http://mirrors.aliyun.com/ubuntu/|g; s|http://security.ubuntu.com/ubuntu/|http://mirrors.aliyun.com/ubuntu/|g' \
    /etc/apt/sources.list.d/ubuntu.sources; \
    fi

RUN apt-get update -y && apt-get upgrade -y

# some dev tools
RUN apt-get install -y \
    git \
    cmake \
    ninja-build \
    build-essential


# install other dependencies
RUN apt-get install -y apt-utils
RUN apt-get install -y rsync openssh-server tmux vim neovim
RUN apt-get install -y curl wget iproute2 iputils-ping proxychains-ng
RUN apt-get install -y htop iftop lsof
RUN apt-get install -y zip unzip

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

# cpp things
RUN apt-get install -y clangd bear

# install python things
RUN apt-get install -y python3 python3-pip python3-dev python3-venv
RUN pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple
RUN echo '\nalias python=python3\nalias pip=pip3\n' >> .bashrc

# common libs
RUN apt-get install -y libsqlite3-dev libomp-dev libnuma-dev

# create default venv
ENV VIRTUAL_ENV=/venv
RUN python3 -m venv $VIRTUAL_ENV
ENV PATH="$VIRTUAL_ENV/bin:$PATH"

# === install xun ===
# install deps ahead of time to leverage Docker layer caching
RUN pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple
RUN pip install --upgrade pip setuptools wheel playwright
RUN playwright install chromium --with-deps

# my extra deps
RUN pip install httpx[socks] 
RUN pip install zai-sdk
RUN pip install pymupdf matplotlib

COPY . /xun-src
WORKDIR /xun-src
RUN pip install .

RUN mkdir -p /workspace
WORKDIR /workspace