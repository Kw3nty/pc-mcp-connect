FROM python:3.13-slim
RUN useradd -m -u 1000 user
WORKDIR /app
COPY requirements.txt relay.py ./
RUN pip install --no-cache-dir -r requirements.txt
USER user
ENV PORT=7860
EXPOSE 7860
CMD ["python", "relay.py"]
