from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel
import sqlite3
from datetime import datetime
import math
import os
import csv
import io
import re

app = FastAPI()

TOTAL_VAGAS = 30
MINUTOS_TOLERANCIA = 15

def get_db():
    conn = sqlite3.connect("database.db")
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    cursor = conn.cursor()
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS movimentacoes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            placa TEXT NOT NULL,
            modelo TEXT DEFAULT '',
            tipo TEXT NOT NULL DEFAULT 'carro',
            data_entrada TEXT NOT NULL,
            data_saida TEXT,
            valor_pago REAL
        )
    """)
    
    # Garantir que a coluna 'modelo' exista em tabelas já criadas
    cursor.execute("PRAGMA table_info(movimentacoes)")
    colunas = [coluna[1] for coluna in cursor.fetchall()]
    if "modelo" not in colunas:
        cursor.execute("ALTER TABLE movimentacoes ADD COLUMN modelo TEXT DEFAULT ''")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS tarifas (
            tipo TEXT PRIMARY KEY,
            valor_hora REAL NOT NULL
        )
    """)
    
    cursor.execute("SELECT COUNT(*) FROM tarifas")
    if cursor.fetchone()[0] == 0:
        cursor.executemany(
            "INSERT INTO tarifas (tipo, valor_hora) VALUES (?, ?)",
            [('carro', 10.0), ('moto', 5.0), ('caminhao', 20.0)]
        )

    conn.commit()
    conn.close()

init_db()

def validar_placa(placa: str) -> bool:
    padrao = r'^[A-Z]{3}-?[0-9]{4}$|^[A-Z]{3}[0-9][A-Z][0-9]{2}$'
    return bool(re.match(padrao, placa))

def obter_tarifas_db():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT tipo, valor_hora FROM tarifas")
    tarifas = {row["tipo"]: row["valor_hora"] for row in cursor.fetchall()}
    conn.close()
    return tarifas

class TabelaTarifa(BaseModel):
    carro: float
    moto: float
    caminhao: float

@app.get("/", response_class=HTMLResponse)
def home():
    caminho_html = os.path.join(os.path.dirname(__file__), "static", "index.html")
    if os.path.exists(caminho_html):
        with open(caminho_html, "r", encoding="utf-8") as f:
            return f.read()
    return "<h1>Erro: Arquivo static/index.html não encontrado.</h1>"

@app.get("/api/tarifas")
def listar_tarifas():
    return obter_tarifas_db()

@app.post("/api/tarifas")
def atualizar_tarifas(dados: TabelaTarifa):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("UPDATE tarifas SET valor_hora = ? WHERE tipo = 'carro'", (dados.carro,))
    cursor.execute("UPDATE tarifas SET valor_hora = ? WHERE tipo = 'moto'", (dados.moto,))
    cursor.execute("UPDATE tarifas SET valor_hora = ? WHERE tipo = 'caminhao'", (dados.caminhao,))
    conn.commit()
    conn.close()
    return {"message": "Tarifas atualizadas com sucesso!"}

@app.get("/api/dashboard")
def obter_dashboard():
    conn = get_db()
    cursor = conn.cursor()
    
    cursor.execute("SELECT COUNT(*) FROM movimentacoes WHERE data_saida IS NULL")
    estacionados = cursor.fetchone()[0]
    
    hoje = datetime.now().strftime("%Y-%m-%d")
    cursor.execute("SELECT SUM(valor_pago), COUNT(*) FROM movimentacoes WHERE data_saida LIKE ?", (f"{hoje}%",))
    res_hoje = cursor.fetchone()
    faturamento_hoje = res_hoje[0] or 0.0
    atendimentos_hoje = res_hoje[1] or 0

    conn.close()
    
    return {
        "vagas_ocupadas": estacionados,
        "vagas_livres": max(0, TOTAL_VAGAS - estacionados),
        "total_vagas": TOTAL_VAGAS,
        "faturamento_hoje": round(faturamento_hoje, 2),
        "atendimentos_hoje": atendimentos_hoje
    }

@app.post("/api/entrada")
def registrar_entrada(placa: str, modelo: str = "", tipo: str = "carro"):
    placa = placa.upper().strip().replace("-", "")
    modelo = modelo.strip().title()
    tipo = tipo.lower()
    tarifas = obter_tarifas_db()
    
    if not validar_placa(placa):
        raise HTTPException(status_code=400, detail="Formato de placa inválido! Use o padrão ABC1D23 ou ABC1234.")
    
    if tipo not in tarifas:
        raise HTTPException(status_code=400, detail="Tipo de veículo inválido.")

    conn = get_db()
    cursor = conn.cursor()
    
    cursor.execute("SELECT COUNT(*) FROM movimentacoes WHERE data_saida IS NULL")
    if cursor.fetchone()[0] >= TOTAL_VAGAS:
        conn.close()
        raise HTTPException(status_code=400, detail="Estacionamento lotado!")

    cursor.execute("SELECT * FROM movimentacoes WHERE placa = ? AND data_saida IS NULL", (placa,))
    if cursor.fetchone():
        conn.close()
        raise HTTPException(status_code=400, detail=f"O veículo {placa} já está no pátio.")

    data_entrada = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute("INSERT INTO movimentacoes (placa, modelo, tipo, data_entrada) VALUES (?, ?, ?, ?)", (placa, modelo, tipo, data_entrada))
    conn.commit()
    conn.close()

    msg_modelo = f" - {modelo}" if modelo else ""
    return {"message": f"Entrada do veículo {placa}{msg_modelo} ({tipo.capitalize()}) registrada com sucesso!"}

@app.get("/api/previa-saida")
def previa_saida(placa: str):
    placa = placa.upper().strip().replace("-", "")
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("SELECT * FROM movimentacoes WHERE placa = ? AND data_saida IS NULL", (placa,))
    registro = cursor.fetchone()
    conn.close()

    if not registro:
        raise HTTPException(status_code=404, detail="Veículo não localizado.")

    tarifas = obter_tarifas_db()
    data_entrada = datetime.strptime(registro["data_entrada"], "%Y-%m-%d %H:%M:%S")
    data_saida = datetime.now()
    
    duracao_segundos = (data_saida - data_entrada).total_seconds()
    minutos_totais = int(duracao_segundos // 60)
    
    if minutos_totais <= MINUTOS_TOLERANCIA:
        valor_total = 0.0
        horas = 0
    else:
        horas = math.ceil(duracao_segundos / 3600)
        tipo_veiculo = registro["tipo"] if "tipo" in registro.keys() and registro["tipo"] else "carro"
        valor_total = horas * tarifas.get(tipo_veiculo, 10.0)

    return {
        "placa": placa,
        "modelo": registro["modelo"] or "",
        "tipo": registro["tipo"],
        "horas": horas,
        "minutos_totais": minutos_totais,
        "valor_estimado": valor_total,
        "tolerancia": minutos_totais <= MINUTOS_TOLERANCIA
    }

@app.post("/api/saida")
def registrar_saida(placa: str):
    placa = placa.upper().strip().replace("-", "")
    tarifas = obter_tarifas_db()

    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("SELECT * FROM movimentacoes WHERE placa = ? AND data_saida IS NULL", (placa,))
    registro = cursor.fetchone()

    if not registro:
        conn.close()
        raise HTTPException(status_code=404, detail=f"Veículo {placa} não encontrado no pátio.")

    data_entrada = datetime.strptime(registro["data_entrada"], "%Y-%m-%d %H:%M:%S")
    data_saida = datetime.now()
    
    duracao_segundos = (data_saida - data_entrada).total_seconds()
    minutos_totais = int(duracao_segundos // 60)
    
    if minutos_totais <= MINUTOS_TOLERANCIA:
        valor_total = 0.0
        horas = 0
    else:
        horas = math.ceil(duracao_segundos / 3600)
        tipo_veiculo = registro["tipo"] if "tipo" in registro.keys() and registro["tipo"] else "carro"
        tarifa_hora = tarifas.get(tipo_veiculo, 10.0)
        valor_total = horas * tarifa_hora

    str_saida = data_saida.strftime("%Y-%m-%d %H:%M:%S")

    cursor.execute(
        "UPDATE movimentacoes SET data_saida = ?, valor_pago = ? WHERE id = ?",
        (str_saida, valor_total, registro["id"])
    )
    conn.commit()
    conn.close()

    return {
        "id": registro["id"],
        "placa": placa,
        "modelo": registro["modelo"] or "",
        "tipo": registro["tipo"],
        "entrada": registro["data_entrada"],
        "saida": str_saida,
        "horas_cobradas": horas,
        "valor_pago": valor_total
    }

@app.delete("/api/movimentacoes/{id}")
def deletar_movimentacao(id: int):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM movimentacoes WHERE id = ?", (id,))
    conn.commit()
    conn.close()
    return {"message": "Registro removido com sucesso."}

@app.get("/api/movimentacoes")
def listar_movimentacoes():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM movimentacoes ORDER BY id DESC")
    linhas = cursor.fetchall()
    conn.close()
    
    resultado = []
    for linha in linhas:
        item = dict(linha)
        if not item.get("tipo"):
            item["tipo"] = "carro"
        if not item.get("modelo"):
            item["modelo"] = ""
        resultado.append(item)
        
    return resultado

@app.get("/api/exportar-csv")
def exportar_csv():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id, placa, modelo, tipo, data_entrada, data_saida, valor_pago FROM movimentacoes ORDER BY id DESC")
    linhas = cursor.fetchall()
    conn.close()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["ID", "Placa", "Modelo", "Tipo", "Data Entrada", "Data Saida", "Valor Pago (R$)"])
    
    for linha in linhas:
        writer.writerow([
            linha["id"], 
            linha["placa"], 
            linha["modelo"] or "-",
            linha["tipo"] or "carro", 
            linha["data_entrada"], 
            linha["data_saida"] or "No Patio", 
            linha["valor_pago"] if linha["valor_pago"] is not None else 0.0
        ])

    return Response(
        content=output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=relatorio_estacionamento.csv"}
    )