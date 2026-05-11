# A reference Dockerfile for xun-box
FROM python:3.12-bookworm

RUN apt-get update 
RUN apt-get install -y git build-essential 
RUN apt-get install -y curl wget iproute2 iputils-ping lsof
RUN apt-get install -y zip unzip

RUN pip install playwright
RUN playwright install chromium --with-deps