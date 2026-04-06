FROM python:3.10-slim
WORKDIR /app
COPY ui_gradio.py ./
RUN pip install --no-cache-dir gradio pandas requests
EXPOSE 7860
CMD ["python", "ui_gradio.py"]