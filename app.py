import streamlit as st
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo
import pandas as pd
from io import BytesIO
import os
import google.generativeai as genai
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload, MediaIoBaseUpload
import numpy as np

# ─────────────────────────────────────────────────────────────
# CONFIGURAÇÃO DA PÁGINA E CSS PERSONALIZADO
# ─────────────────────────────────────────────────────────────
st.set_page_config(page_title="Controle de Estoque WMS", page_icon="📦", layout="wide")

# CSS para modernizar a interface (Botões e Tabelas) sem quebrar o padrão
st.markdown("""
    <style>
    .stButton>button {
        border-radius: 8px;
        font-weight: 600;
        transition: all 0.2s ease-in-out;
    }
    .stButton>button:hover {
        transform: scale(1.02);
    }
    .metric-card {
        background-color: #f8f9fa;
        padding: 15px;
        border-radius: 10px;
        border-left: 5px solid #0052cc;
        box-shadow: 2px 2px 5px rgba(0,0,0,0.05);
    }
    </style>
""", unsafe_allow_html=True)

DB_PATH = "estoque.db"
FOLDER_ID = st.secrets["FOLDER_ID"]

# ─────────────────────────────────────────────────────────────
# CONEXÃO E SINCRONIZAÇÃO COM GOOGLE DRIVE
# ─────────────────────────────────────────────────────────────
def obter_servico_drive():
    info_chaves = dict(st.secrets["gcp_service_account"])
    credenciais = service_account.Credentials.from_service_account_info(info_chaves)
    return build('drive', 'v3', credentials=credenciais)

def descarregar_do_drive():
    try:
        servico = obter_servico_drive()
        query = f"name='{DB_PATH}' and '{FOLDER_ID}' in parents and trashed=false"
        resultados = servico.files().list(q=query, fields="files(id)").execute()
        if resultados.get('files', []):
            id_ficheiro = resultados['files'][0]['id']
            requisicao = servico.files().get_media(fileId=id_ficheiro)
            with open(DB_PATH, "wb") as f:
                carregador = MediaIoBaseDownload(f, requisicao)
                concluido = False
                while not concluido:
                    _, concluido = carregador.next_chunk()
            return True
    except Exception as e:
        st.error(f"Erro ao descarregar do Google Drive: {e}")
    return False

def enviar_para_o_drive():
    try:
        servico = obter_servico_drive()
        query = f"name='{DB_PATH}' and '{FOLDER_ID}' in parents and trashed=false"
        ficheiros = servico.files().list(q=query, fields="files(id)").execute().get('files', [])
        media = MediaFileUpload(DB_PATH, mimetype='application/x-sqlite3', resumable=True)
        if ficheiros:
            servico.files().update(fileId=ficheiros[0]['id'], media_body=media).execute()
        else:
            servico.files().create(body={'name': DB_PATH, 'parents': [FOLDER_ID]}, media_body=media).execute()
    except Exception as e:
        st.error(f"Erro ao enviar banco para o Drive: {e}")

def sincronizar_csv_drive(df, nome_arquivo):
    try:
        servico = obter_servico_drive()
        query = f"name='{nome_arquivo}' and '{FOLDER_ID}' in parents and trashed=false"
        ficheiros = servico.files().list(q=query, fields="files(id)").execute().get('files', [])
        csv_bytes = df.to_csv(index=False).encode('utf-8-sig')
        media = MediaIoBaseUpload(BytesIO(csv_bytes), mimetype='text/csv', resumable=True)
        if ficheiros:
            servico.files().update(fileId=ficheiros[0]['id'], media_body=media).execute()
        else:
            servico.files().create(body={'name': nome_arquivo, 'parents': [FOLDER_ID]}, media_body=media).execute()
    except Exception:
        pass

def sincronizar_tudo():
    enviar_para_o_drive()
    sincronizar_csv_drive(listar_produtos(), "produtos_looker.csv")
    sincronizar_csv_drive(listar_movimentacoes(), "movimentacoes_looker.csv")

# ─────────────────────────────────────────────────────────────
# EXPORTAÇÕES LOCAIS
# ─────────────────────────────────────────────────────────────
@st.cache_data
def converter_para_csv(df):
    return df.to_csv(index=False).encode("utf-8-sig")

def converter_para_excel(df):
    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Movimentacoes")
    return output.getvalue()

# ─────────────────────────────────────────────────────────────
# BANCO DE DADOS E LÓGICA (INTACTO PARA NÃO QUEBRAR O BI)
# ─────────────────────────────────────────────────────────────
def get_conn():
    return sqlite3.connect(DB_PATH, check_same_thread=False)

def init_db():
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS produtos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nome TEXT NOT NULL UNIQUE,
                saldo_atual INTEGER NOT NULL DEFAULT 0,
                estoque_minimo INTEGER DEFAULT 10,
                valor_unitario REAL DEFAULT 0,
                categoria TEXT DEFAULT 'Geral',
                lead_time INTEGER DEFAULT 3
            );
            CREATE TABLE IF NOT EXISTS movimentacoes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                id_produto INTEGER NOT NULL REFERENCES produtos(id),
                data_hora TEXT NOT NULL,
                tipo TEXT NOT NULL CHECK(tipo IN ('Entrada','Saída','Ajuste','Contagem')),
                quantidade INTEGER NOT NULL,
                saldo_resultante INTEGER NOT NULL,
                observacao TEXT
            );
        """)
        cursor = conn.execute("PRAGMA table_info(produtos)")
        colunas = [col[1] for col in cursor.fetchall()]
        if 'categoria' not in colunas: conn.execute("ALTER TABLE produtos ADD COLUMN categoria TEXT DEFAULT 'Geral'")
        if 'lead_time' not in colunas: conn.execute("ALTER TABLE produtos ADD COLUMN lead_time INTEGER DEFAULT 3")

def listar_produtos():
    with get_conn() as conn: return pd.read_sql("SELECT * FROM produtos ORDER BY nome", conn)

def listar_movimentacoes():
    with get_conn() as conn:
        return pd.read_sql("""
            SELECT m.id, p.nome AS produto, m.data_hora, m.tipo, m.quantidade, m.saldo_resultante, m.observacao
            FROM movimentacoes m JOIN produtos p ON p.id = m.id_produto ORDER BY m.id DESC
        """, conn)

def atualizar_saldo(conn, id_produto, novo_saldo):
    conn.execute("UPDATE produtos SET saldo_atual = ? WHERE id = ?", (novo_saldo, id_produto))

def registrar_movimentacao(conn, id_produto, tipo, quantidade, saldo_resultante, obs):
    data_hora = datetime.now(ZoneInfo("America/Fortaleza")).strftime("%d/%m/%Y %H:%M")
    conn.execute("INSERT INTO movimentacoes (id_produto, data_hora, tipo, quantidade, saldo_resultante, observacao) VALUES (?, ?, ?, ?, ?, ?)", (id_produto, data_hora, tipo, quantidade, saldo_resultante, obs))

def cadastrar_produto(nome, estoque_minimo, valor_unitario, categoria, lead_time):
    try:
        with get_conn() as conn:
            conn.execute("INSERT INTO produtos (nome, saldo_atual, estoque_minimo, valor_unitario, categoria, lead_time) VALUES (?, 0, ?, ?, ?, ?)", (nome, estoque_minimo, valor_unitario, categoria, lead_time))
        return True, "Cadastrado com sucesso."
    except sqlite3.IntegrityError: return False, "Produto já existe."

def editar_produto(id_produto, nome, estoque_minimo, valor_unitario, categoria, lead_time):
    try:
        with get_conn() as conn:
            conn.execute("UPDATE produtos SET nome = ?, estoque_minimo = ?, valor_unitario = ?, categoria = ?, lead_time = ? WHERE id = ?", (nome, estoque_minimo, valor_unitario, categoria, lead_time, id_produto))
        return True, "Atualizado."
    except sqlite3.IntegrityError: return False, "Nome em uso."

def deletar_produto(id_produto):
    with get_conn() as conn:
        conn.execute("DELETE FROM movimentacoes WHERE id_produto = ?", (id_produto,))
        conn.execute("DELETE FROM produtos WHERE id = ?", (id_produto,))

# ─────────────────────────────────────────────────────────────
# INICIALIZAÇÃO
# ─────────────────────────────────────────────────────────────
if "db_sincronizado" not in st.session_state:
    if not descarregar_do_drive(): sincronizar_tudo()
    init_db()
    st.session_state["db_sincronizado"] = True

if "alerta_ruptura" not in st.session_state: st.session_state["alerta_ruptura"] = None

# ─────────────────────────────────────────────────────────────
# INTERFACE
# ─────────────────────────────────────────────────────────────
st.title("📦 Sistema de Gestão de Insumos")
st.caption("Operação Logística Inteligente | Controle Dinâmico WMS")

if st.session_state["alerta_ruptura"]:
    st.error(st.session_state["alerta_ruptura"], icon="🚨")
    st.session_state["alerta_ruptura"] = None

aba_painel, aba_operacao, aba_ia, aba_historico, aba_cadastro = st.tabs([
    "📊 Painel WMS", "⚡ Operação", "🧠 Assistente IA", "📜 Histórico", "⚙️ Gestão de Produtos"
])

# ═════════════════════════════════════════════════════════════
# PAINEL WMS (COM FÓRMULAS PREDITIVAS)
# ═════════════════════════════════════════════════════════════
with aba_painel:
    produtos_df = listar_produtos()
    movs_df = listar_movimentacoes()

    if not produtos_df.empty:
        produtos_df["valor_total"] = produtos_df["saldo_atual"] * produtos_df["valor_unitario"]
        
        # --- LÓGICA DE CURVA ABC ---
        df_abc = produtos_df[produtos_df["valor_total"] > 0].copy()
        if not df_abc.empty:
            df_abc = df_abc.sort_values(by="valor_total", ascending=False)
            df_abc["perc_acumulado"] = (df_abc["valor_total"] / df_abc["valor_total"].sum()).cumsum()
            df_abc["Curva ABC"] = df_abc["perc_acumulado"].apply(lambda x: 'A' if x<=0.8 else ('B' if x<=0.95 else 'C'))
            produtos_df = produtos_df.merge(df_abc[["id", "Curva ABC"]], on="id", how="left").fillna({"Curva ABC": "-"})
        else: produtos_df["Curva ABC"] = "-"

        # --- CÁLCULO PREDITIVO DE COBERTURA (RUNWAY) E WMS ---
        with get_conn() as conn:
            df_compra = pd.read_sql("""
                SELECT p.id, COALESCE(SUM(ABS(m.quantidade)), 0) AS consumo_total
                FROM produtos p LEFT JOIN movimentacoes m ON p.id = m.id_produto AND m.tipo = 'Saída' GROUP BY p.id
            """, conn)
        
        # Junta os dados de consumo à tabela de produtos principal
        produtos_df = produtos_df.merge(df_compra, on="id", how="left")
        
        # Fórmulas Avançadas processadas no Pandas (Sem tocar no DB)
        produtos_df["consumo_diario"] = produtos_df["consumo_total"] / 30
        
        # Cobertura de Estoque (Dias para Esgotar)
        produtos_df["Dias de Cobertura"] = np.where(produtos_df["consumo_diario"] > 0, (produtos_df["saldo_atual"] / produtos_df["consumo_diario"]).astype(int), 999)
        produtos_df["Dias de Cobertura"] = produtos_df["Dias de Cobertura"].apply(lambda x: "Sem consumo" if x == 999 else f"{x} dias")

        # Semáforo Melhorado (Baseado também na Cobertura)
        def status_avancado(row):
            if row['saldo_atual'] <= 0: return '🔴 Zerado'
            elif row['saldo_atual'] < row['estoque_minimo']: return '🔴 Crítico'
            elif row['Dias de Cobertura'] != "Sem consumo" and int(row['Dias de Cobertura'].split()[0]) <= row['lead_time']: return '🟠 Risco de Ruptura'
            elif row['saldo_atual'] <= (row['estoque_minimo'] * 1.3): return '🟡 Atenção'
            return '🟢 Saudável'
        
        produtos_df['Status'] = produtos_df.apply(status_avancado, axis=1)

        # Cartões Visuais
        c1, c2, c3, c4 = st.columns(4)
        c1.markdown(f'<div class="metric-card"><h4>📦 Produtos</h4><h2>{len(produtos_df)}</h2></div>', unsafe_allow_html=True)
        c2.markdown(f'<div class="metric-card"><h4>⚙️ Volume Total</h4><h2>{int(produtos_df["saldo_atual"].sum())} un</h2></div>', unsafe_allow_html=True)
        c3.markdown(f'<div class="metric-card"><h4>🚨 Itens Críticos</h4><h2>{int((produtos_df["saldo_atual"] < produtos_df["estoque_minimo"]).sum())}</h2></div>', unsafe_allow_html=True)
        c4.markdown(f'<div class="metric-card"><h4>💰 Valor Bloqueado</h4><h2>R$ {produtos_df["valor_total"].sum():,.2f}</h2></div>', unsafe_allow_html=True)

        st.divider()
        col1, col2 = st.columns(2, gap="large")

        with col1:
            st.markdown("### 📊 Monitoramento em Tempo Real")
            st.dataframe(produtos_df[["Status", "categoria", "nome", "Curva ABC", "saldo_atual", "Dias de Cobertura"]].rename(columns={"categoria": "Setor", "nome": "Produto", "saldo_atual": "Saldo", "Dias de Cobertura": "Runway"}), hide_index=True)

        with col2:
            st.markdown("### 🛒 Inteligência de Ressuprimento")
            # Fórmulas Mínimo Ideal WMS = Consumo diário * Lead Time * Margem Segurança (1.2)
            produtos_df["Minimo Ideal"] = (produtos_df["consumo_diario"] * produtos_df["lead_time"] * 1.2).astype(int)
            produtos_df["Alvo"] = produtos_df[["estoque_minimo", "Minimo Ideal"]].max(axis=1)
            produtos_df["Sugestão Compra"] = (produtos_df["Alvo"] - produtos_df["saldo_atual"]).clip(lower=0)
            
            st.dataframe(produtos_df[["categoria", "nome", "lead_time", "saldo_atual", "Minimo Ideal", "Sugestão Compra"]].rename(columns={"categoria": "Setor", "nome": "Produto", "lead_time": "Entrega(d)", "saldo_atual": "Saldo", "Sugestão Compra": "Comprar"}), hide_index=True)

# ═════════════════════════════════════════════════════════════
# OPERAÇÃO LOGÍSTICA (DESIGN LIMPO E RÁPIDO)
# ═════════════════════════════════════════════════════════════
with aba_operacao:
    st.markdown("### ⚡ Fast-Track Operacional")
    st.caption("Ações rápidas de chão de fábrica")
    
    col_saida, col_entrada = st.columns(2)
    
    with col_saida:
        with st.container(border=True):
            st.subheader("📤 Registrar Saída")
            produtos_df = listar_produtos()
            if not produtos_df.empty:
                opcoes = dict(zip(produtos_df["nome"], produtos_df["id"]))
                nome_sel_s = st.selectbox("Produto", list(opcoes.keys()), key="s_prod")
                id_sel_s = opcoes[nome_sel_s]
                saldo_atual_s = int(produtos_df.loc[produtos_df["id"]==id_sel_s, "saldo_atual"].values[0])
                minimo_s = int(produtos_df.loc[produtos_df["id"]==id_sel_s, "estoque_minimo"].values[0])
                
                c1, c2 = st.columns([1, 2])
                with c1: qty_s = st.number_input("Qtd", min_value=1, max_value=max(saldo_atual_s, 1), step=1, key="s_qtd")
                with c2: obs_s = st.text_input("Observação", key="s_obs")
                
                if st.button("Confirmar Saída ➔", type="primary", use_container_width=True):
                    novo_s = saldo_atual_s - int(qty_s)
                    if novo_s < minimo_s: st.session_state["alerta_ruptura"] = f"Atenção: O saldo de '{nome_sel_s}' entrou em nível crítico!"
                    with get_conn() as conn:
                        atualizar_saldo(conn, id_sel_s, novo_s)
                        registrar_movimentacao(conn, id_sel_s, "Saída", -int(qty_s), novo_s, obs_s)
                    with st.spinner("Sincronizando..."): sincronizar_tudo()
                    st.rerun()

    with col_entrada:
        with st.container(border=True):
            st.subheader("📥 Registrar Entrada")
            if not produtos_df.empty:
                nome_sel_e = st.selectbox("Produto", list(opcoes.keys()), key="e_prod")
                id_sel_e = opcoes[nome_sel_e]
                saldo_atual_e = int(produtos_df.loc[produtos_df["id"]==id_sel_e, "saldo_atual"].values[0])
                
                c1, c2 = st.columns([1, 2])
                with c1: qty_e = st.number_input("Qtd", min_value=1, step=1, key="e_qtd")
                with c2: obs_e = st.text_input("Nota/Fornecedor", key="e_obs")
                
                if st.button("Confirmar Entrada ➔", type="secondary", use_container_width=True):
                    novo_e = saldo_atual_e + int(qty_e)
                    with get_conn() as conn:
                        atualizar_saldo(conn, id_sel_e, novo_e)
                        registrar_movimentacao(conn, id_sel_e, "Entrada", int(qty_e), novo_e, obs_e)
                    with st.spinner("Sincronizando..."): sincronizar_tudo()
                    st.rerun()
                    
    with st.expander("🔧 Funções Especiais (Ajuste de Inventário / Contagem)"):
        c_ajuste, c_contagem = st.columns(2)
        with c_ajuste:
            st.markdown("**Ajuste de Saldo**")
            nome_sel_a = st.selectbox("Produto", list(opcoes.keys()), key="a_prod")
            id_sel_a = opcoes[nome_sel_a]
            saldo_a = int(produtos_df.loc[produtos_df["id"]==id_sel_a, "saldo_atual"].values[0])
            novo_a = st.number_input("Novo Saldo Real", min_value=0, value=saldo_a, step=1, key="a_qtd")
            obs_a = st.text_input("Motivo do Ajuste", key="a_obs")
            if st.button("Aplicar Ajuste"):
                with get_conn() as conn:
                    atualizar_saldo(conn, id_sel_a, novo_a)
                    registrar_movimentacao(conn, id_sel_a, "Ajuste", novo_a-saldo_a, novo_a, obs_a)
                with st.spinner("Sincronizando..."): sincronizar_tudo()
                st.rerun()
                
        with c_contagem:
            st.markdown("**Contagem Semanal**")
            nome_sel_c = st.selectbox("Produto", list(opcoes.keys()), key="c_prod")
            id_sel_c = opcoes[nome_sel_c]
            saldo_c = int(produtos_df.loc[produtos_df["id"]==id_sel_c, "saldo_atual"].values[0])
            fisico_c = st.number_input("Estoque Físico", min_value=0, step=1, key="c_qtd")
            if st.button("Gravar Contagem"):
                with get_conn() as conn:
                    atualizar_saldo(conn, id_sel_c, fisico_c)
                    registrar_movimentacao(conn, id_sel_c, "Contagem", fisico_c-saldo_c, fisico_c, "Inventário de Rotina")
                with st.spinner("Sincronizando..."): sincronizar_tudo()
                st.rerun()

# ═════════════════════════════════════════════════════════════
# ASSISTENTE IA (GEMINI)
# ═════════════════════════════════════════════════════════════
with aba_ia:
    with st.container(border=True):
        st.subheader("🤖 Analista Logístico Virtual")
        st.markdown("A IA cruza o saldo atual, estoque mínimo e lead time para prever rupturas.")
        if st.button("✨ Executar Auditoria de Estoque", type="primary"):
            produtos_df = listar_produtos()
            if not produtos_df.empty:
                with st.spinner("Processando dados logísticos na nuvem..."):
                    try:
                        genai.configure(api_key=st.secrets["GEMINI_API_KEY"])
                        modelos = [m.name for m in genai.list_models() if 'generateContent' in m.supported_generation_methods]
                        mod = next((m for m in modelos if 'flash' in m or 'pro' in m), modelos[0])
                        dados = produtos_df[["categoria", "nome", "saldo_atual", "estoque_minimo", "lead_time"]].to_string(index=False)
                        prompt = f"Analise o estoque logístico:\n{dados}\nMe dê: Resumo, alertas críticos de risco de ruptura antes da reposição e recomendação tática."
                        st.write(genai.GenerativeModel(mod).generate_content(prompt).text)
                    except Exception as e: st.error(f"Erro na IA: {e}")

# ═════════════════════════════════════════════════════════════
# HISTÓRICO
# ═════════════════════════════════════════════════════════════
with aba_historico:
    movs_df = listar_movimentacoes()
    if not movs_df.empty:
        st.dataframe(movs_df, hide_index=True, use_container_width=True)
        st.download_button("📥 Baixar Matriz de Dados (CSV)", converter_para_csv(movs_df), "movs.csv", "text/csv")

# ═════════════════════════════════════════════════════════════
# GESTÃO DE PRODUTOS
# ═════════════════════════════════════════════════════════════
with aba_cadastro:
    aba_novo, aba_editar, aba_excluir = st.tabs(["➕ Novo Cadastro", "✏️ Atualizar Dados", "🗑️ Eliminar"])
    
    with aba_novo:
        cat = st.selectbox("Setor Operacional", ["Limpeza", "Copa", "Escritório", "EPI", "Operação", "Geral"])
        nome = st.text_input("Descrição do Insumo")
        c1, c2, c3 = st.columns(3)
        with c1: minimo = st.number_input("Estoque Mínimo", min_value=0, value=10)
        with c2: lead = st.number_input("Lead Time (Dias p/ Entrega)", min_value=1, value=3)
        with c3: valor = st.number_input("Custo Unitário (R$)", min_value=0.0, format="%.2f")
        if st.button("Gravar Insumo", type="primary"):
            if nome.strip():
                ok, msg = cadastrar_produto(nome.strip(), minimo, valor, cat, lead)
                if ok:
                    with st.spinner("Sincronizando..."): sincronizar_tudo()
                    st.rerun()
                else: st.error(msg)
    
    with aba_editar:
        produtos_df = listar_produtos()
        if not produtos_df.empty:
            opcoes_edit = dict(zip(produtos_df["nome"], produtos_df["id"]))
            nome_edit = st.selectbox("Selecione o insumo", list(opcoes_edit.keys()), key="sel_edit")
            id_edit = opcoes_edit[nome_edit]
            
            prod_atual = produtos_df[produtos_df["id"] == id_edit].iloc[0]
            lista_cats = ["Geral", "Limpeza", "Copa", "Escritório", "EPI", "Operação", "Outros"]
            if prod_atual["categoria"] not in lista_cats: lista_cats.append(prod_atual["categoria"])
                
            nova_cat = st.selectbox("Setor Operacional", lista_cats, index=lista_cats.index(prod_atual["categoria"]), key="ed_cat")
            novo_nome = st.text_input("Descrição", value=prod_atual["nome"], key="ed_nome")
            c1, c2, c3 = st.columns(3)
            with c1: novo_min = st.number_input("Estoque Mínimo", min_value=0, value=int(prod_atual["estoque_minimo"]), key="ed_min")
            with c2: novo_lead = st.number_input("Lead Time", min_value=1, value=int(prod_atual["lead_time"]), key="ed_lead")
            with c3: novo_val = st.number_input("Custo Unitário", min_value=0.0, value=float(prod_atual["valor_unitario"]), format="%.2f", key="ed_val")
            
            if st.button("✏️ Confirmar Atualização", type="primary"):
                if novo_nome.strip():
                    ok, msg = editar_produto(id_edit, novo_nome.strip(), novo_min, novo_val, nova_cat, novo_lead)
                    if ok:
                        with st.spinner("Sincronizando..."): sincronizar_tudo()
                        st.rerun()
                    else: st.error(msg)
        else: st.info("Inventário vazio.")

    with aba_excluir:
        if not produtos_df.empty:
            del_nome = st.selectbox("Insumo a remover da base", list(dict(zip(produtos_df["nome"], produtos_df["id"])).keys()))
            st.error("Aviso: Esta ação destruirá o histórico associado.")
            if st.button("🗑️ Eliminar Definitivamente"):
                deletar_produto(dict(zip(produtos_df["nome"], produtos_df["id"]))[del_nome])
                with st.spinner("Sincronizando..."): sincronizar_tudo()
                st.rerun()