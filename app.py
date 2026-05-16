import streamlit as st
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo
import pandas as pd
from io import BytesIO

# ─────────────────────────────────────────────────────────────
# CONFIGURAÇÃO DA PÁGINA
# ─────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Controle de Estoque",
    page_icon="📦",
    layout="wide",
)

DB_PATH = "estoque.db"

# ─────────────────────────────────────────────────────────────
# FUNÇÕES DE EXPORTAÇÃO
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
# BANCO DE DADOS E LÓGICA
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
                valor_unitario REAL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS movimentacoes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                id_produto INTEGER NOT NULL REFERENCES produtos(id),
                data_hora TEXT NOT NULL,
                tipo TEXT NOT NULL
                    CHECK(tipo IN ('Entrada','Saída','Ajuste','Contagem')),
                quantidade INTEGER NOT NULL,
                saldo_resultante INTEGER NOT NULL,
                observacao TEXT
            );
        """)
        cursor = conn.execute("SELECT COUNT(*) FROM produtos")
        if cursor.fetchone()[0] == 0:
            conn.executemany("""
                INSERT INTO produtos (nome, saldo_atual, estoque_minimo, valor_unitario)
                VALUES (?, ?, ?, ?)
            """, [
                ("Papel higiênico", 120, 50, 1.80),
                ("Sabonete líquido", 30, 10, 12.50),
                ("Desinfetante", 20, 10, 8.90),
                ("Saco de lixo", 80, 30, 0.90),
            ])

def listar_produtos():
    with get_conn() as conn:
        return pd.read_sql("SELECT * FROM produtos ORDER BY nome", conn)

def listar_movimentacoes():
    with get_conn() as conn:
        return pd.read_sql("""
            SELECT
                m.id,
                p.nome AS produto,
                m.data_hora,
                m.tipo,
                m.quantidade,
                m.saldo_resultante,
                m.observacao
            FROM movimentacoes m
            JOIN produtos p ON p.id = m.id_produto
            ORDER BY m.id DESC
        """, conn)

def atualizar_saldo(conn, id_produto, novo_saldo):
    conn.execute("UPDATE produtos SET saldo_atual = ? WHERE id = ?", (novo_saldo, id_produto))

def registrar_movimentacao(conn, id_produto, tipo, quantidade, saldo_resultante, obs):
    # Fuso horário blindado para o Nordeste do Brasil
    data_hora = datetime.now(ZoneInfo("America/Fortaleza")).strftime("%d/%m/%Y %H:%M")
    conn.execute("""
        INSERT INTO movimentacoes (id_produto, data_hora, tipo, quantidade, saldo_resultante, observacao)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (id_produto, data_hora, tipo, quantidade, saldo_resultante, obs))

def cadastrar_produto(nome, estoque_minimo, valor_unitario):
    try:
        with get_conn() as conn:
            conn.execute("""
                INSERT INTO produtos (nome, saldo_atual, estoque_minimo, valor_unitario)
                VALUES (?, 0, ?, ?)
            """, (nome, estoque_minimo, valor_unitario))
        return True, "Produto cadastrado com sucesso."
    except sqlite3.IntegrityError:
        return False, "Produto já existe."

def deletar_produto(id_produto):
    with get_conn() as conn:
        conn.execute("DELETE FROM movimentacoes WHERE id_produto = ?", (id_produto,))
        conn.execute("DELETE FROM produtos WHERE id = ?", (id_produto,))

# ─────────────────────────────────────────────────────────────
# INICIALIZAÇÃO
# ─────────────────────────────────────────────────────────────
init_db()

# ─────────────────────────────────────────────────────────────
# INTERFACE PRINCIPAL
# ─────────────────────────────────────────────────────────────
st.title("📦 Controle de Estoque")
st.caption("Controle de insumos de limpeza e operações")
st.divider()

aba_painel, aba_entrada, aba_saida, aba_ajuste, aba_contagem, aba_historico, aba_cadastro = st.tabs([
    "📊 Painel", "⬇️ Entrada", "⬆️ Saída", "🔧 Ajuste", "📋 Contagem", "📜 Histórico", "➕ Produtos"
])

# ═════════════════════════════════════════════════════════════
# PAINEL
# ═════════════════════════════════════════════════════════════
with aba_painel:
    produtos_df = listar_produtos()
    movs_df = listar_movimentacoes()

    if not produtos_df.empty:
        produtos_df["valor_total"] = produtos_df["saldo_atual"] * produtos_df["valor_unitario"]
        total_itens = len(produtos_df)
        total_saldo = int(produtos_df["saldo_atual"].sum())
        saldo_baixo = int((produtos_df["saldo_atual"] < produtos_df["estoque_minimo"]).sum())
        valor_total_estoque = produtos_df["valor_total"].sum()
        total_movs = len(movs_df)

        st.markdown("### Visão Geral")
        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("Produtos", total_itens)
        c2.metric("Total estoque", total_saldo)
        c3.metric("Estoque crítico", saldo_baixo)
        c4.metric("Movimentações", total_movs)
        c5.metric("Valor estoque", f"R$ {valor_total_estoque:,.2f}".replace(",", "X").replace(".", ",").replace("X", "."))

        st.divider()

        # Adicionamos um gap (espaço) maior entre as duas colunas
        col1, col2 = st.columns(2, gap="large")

        with col1:
            st.markdown("### 📦 Posição de Estoque")
            st.dataframe(
                produtos_df[["nome", "saldo_atual", "estoque_minimo", "valor_unitario", "valor_total"]].rename(columns={
                    "nome": "Produto", "saldo_atual": "Saldo", "estoque_minimo": "Mínimo", 
                    "valor_unitario": "Valor Un.", "valor_total": "Total"
                }),
                width="stretch", hide_index=True
            )

        with col2:
            st.markdown("### 🛒 Sugestão de Compras")
            with get_conn() as conn:
                df_compras = pd.read_sql("""
                    SELECT p.nome AS Produto, p.saldo_atual AS saldo_atual, p.estoque_minimo,
                           COALESCE(SUM(ABS(m.quantidade)), 0) AS consumo
                    FROM produtos p
                    LEFT JOIN movimentacoes m ON p.id = m.id_produto AND m.tipo = 'Saída'
                    GROUP BY p.id
                """, conn)

            df_compras["consumo_medio"] = df_compras["consumo"] / 4
            df_compras["Sugestão Compra"] = ((df_compras["consumo_medio"] * 4) + df_compras["estoque_minimo"] - df_compras["saldo_atual"]).clip(lower=0)

            # Agora devidamente alinhado dentro do col2 e com colunas renomeadas de forma mais compacta
            st.dataframe(
                df_compras[["Produto", "consumo", "consumo_medio", "estoque_minimo", "saldo_atual", "Sugestão Compra"]].rename(columns={
                    "saldo_atual": "Saldo", "consumo": "Consumo", "consumo_medio": "Média/Sem", 
                    "estoque_minimo": "Mínimo", "Sugestão Compra": "Comprar"
                }),
                width="stretch", hide_index=True
            )

        st.divider()
        st.markdown("### 📊 Produtos mais consumidos")
        
        # Proteção: Só gera gráfico se houver saídas registradas
        if not movs_df.empty:
            saidas_df = movs_df[movs_df["tipo"] == "Saída"]
            if not saidas_df.empty:
                grafico = saidas_df.groupby("produto")["quantidade"].sum().abs().sort_values(ascending=False)
                st.bar_chart(grafico)
            else:
                st.info("Ainda não há saídas registradas para gerar o gráfico.")
    else:
        st.info("Cadastre produtos para visualizar o painel.")

# ═════════════════════════════════════════════════════════════
# ENTRADA
# ═════════════════════════════════════════════════════════════
with aba_entrada:
    st.subheader("Registrar Entrada")
    produtos_df = listar_produtos()
    if produtos_df.empty:
        st.warning("⚠️ Nenhum produto cadastrado. Vá até a aba 'Produtos' primeiro.")
    else:
        opcoes = dict(zip(produtos_df["nome"], produtos_df["id"]))
        nome_sel = st.selectbox("Produto", list(opcoes.keys()), key="ent_prod")
        id_sel = opcoes[nome_sel]
        saldo_atual = int(produtos_df.loc[produtos_df["id"] == id_sel, "saldo_atual"].values[0])

        col1, col2 = st.columns(2)
        with col1:
            qty = st.number_input("Quantidade", min_value=1, step=1, key="ent_qty")
        with col2:
            obs = st.text_input("Observação", key="ent_obs")

        st.info(f"Saldo atual: **{saldo_atual}** → Novo saldo: **{saldo_atual + int(qty)}**")

        if st.button("✅ Registrar Entrada", type="primary"):
            novo_saldo = saldo_atual + int(qty)
            with get_conn() as conn:
                atualizar_saldo(conn, id_sel, novo_saldo)
                registrar_movimentacao(conn, id_sel, "Entrada", int(qty), novo_saldo, obs)
            st.success("Entrada registrada com sucesso!")
            st.rerun()

# ═════════════════════════════════════════════════════════════
# SAÍDA
# ═════════════════════════════════════════════════════════════
with aba_saida:
    st.subheader("Registrar Saída")
    produtos_df = listar_produtos()
    if produtos_df.empty:
        st.warning("⚠️ Nenhum produto cadastrado.")
    else:
        opcoes = dict(zip(produtos_df["nome"], produtos_df["id"]))
        nome_sel = st.selectbox("Produto", list(opcoes.keys()), key="sai_prod")
        id_sel = opcoes[nome_sel]
        saldo_atual = int(produtos_df.loc[produtos_df["id"] == id_sel, "saldo_atual"].values[0])

        col1, col2 = st.columns(2)
        with col1:
            qty = st.number_input("Quantidade", min_value=1, max_value=max(saldo_atual, 1), step=1, key="sai_qty")
        with col2:
            obs = st.text_input("Observação", key="sai_obs")

        if st.button("✅ Registrar Saída", type="primary"):
            novo_saldo = saldo_atual - int(qty)
            with get_conn() as conn:
                atualizar_saldo(conn, id_sel, novo_saldo)
                registrar_movimentacao(conn, id_sel, "Saída", -int(qty), novo_saldo, obs)
            st.success("Saída registrada com sucesso!")
            st.rerun()

# ═════════════════════════════════════════════════════════════
# AJUSTE
# ═════════════════════════════════════════════════════════════
with aba_ajuste:
    st.subheader("Ajuste de Estoque")
    produtos_df = listar_produtos()
    if produtos_df.empty:
        st.warning("⚠️ Nenhum produto cadastrado.")
    else:
        opcoes = dict(zip(produtos_df["nome"], produtos_df["id"]))
        nome_sel = st.selectbox("Produto", list(opcoes.keys()), key="aju_prod")
        id_sel = opcoes[nome_sel]
        saldo_atual = int(produtos_df.loc[produtos_df["id"] == id_sel, "saldo_atual"].values[0])

        col1, col2 = st.columns(2)
        with col1:
            novo_saldo = st.number_input("Novo saldo", min_value=0, step=1, value=saldo_atual, key="aju_qty")
        with col2:
            obs = st.text_input("Motivo", key="aju_obs")

        diferenca = int(novo_saldo) - saldo_atual
        if st.button("✅ Aplicar Ajuste", type="primary"):
            with get_conn() as conn:
                atualizar_saldo(conn, id_sel, int(novo_saldo))
                registrar_movimentacao(conn, id_sel, "Ajuste", diferenca, int(novo_saldo), obs)
            st.success("Ajuste realizado com sucesso!")
            st.rerun()

# ═════════════════════════════════════════════════════════════
# CONTAGEM
# ═════════════════════════════════════════════════════════════
with aba_contagem:
    st.subheader("Inventário / Contagem")
    produtos_df = listar_produtos()
    if produtos_df.empty:
        st.warning("⚠️ Nenhum produto cadastrado.")
    else:
        opcoes = dict(zip(produtos_df["nome"], produtos_df["id"]))
        nome_sel = st.selectbox("Produto", list(opcoes.keys()), key="cnt_prod")
        id_sel = opcoes[nome_sel]
        saldo_sistemico = int(produtos_df.loc[produtos_df["id"] == id_sel, "saldo_atual"].values[0])

        estoque_fisico = st.number_input("Estoque físico (contado)", min_value=0, step=1, key="cnt_qty")
        consumo = saldo_sistemico - int(estoque_fisico)
        divergencia_pct = (abs(consumo) / saldo_sistemico * 100) if saldo_sistemico > 0 else 0

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Sistema", saldo_sistemico)
        c2.metric("Físico", estoque_fisico)
        c3.metric("Diferença", consumo)
        c4.metric("Divergência %", f"{divergencia_pct:.1f}%")

        if st.button("✅ Registrar Contagem", type="primary"):
            with get_conn() as conn:
                atualizar_saldo(conn, id_sel, estoque_fisico)
                registrar_movimentacao(conn, id_sel, "Contagem", -consumo, estoque_fisico, "Contagem semanal")
            st.success("Contagem registrada!")
            st.rerun()

# ═════════════════════════════════════════════════════════════
# HISTÓRICO E EXPORTAÇÃO
# ═════════════════════════════════════════════════════════════
with aba_historico:
    st.subheader("Histórico de Movimentações")
    movs_df = listar_movimentacoes()

    if movs_df.empty:
        st.info("Nenhuma movimentação registrada.")
    else:
        st.dataframe(
            movs_df.rename(columns={
                "id": "ID", "produto": "Produto", "data_hora": "Data/Hora",
                "tipo": "Tipo", "quantidade": "Quantidade", "saldo_resultante": "Saldo", "observacao": "Observação"
            }),
            width="stretch", hide_index=True
        )

        st.divider()
        col1, col2 = st.columns(2)
        
        with col1:
            csv_data = converter_para_csv(movs_df)
            st.download_button(
                label="📥 Baixar em CSV", data=csv_data, 
                file_name=f"movimentacoes_{datetime.now().strftime('%Y%m%d')}.csv", mime="text/csv"
            )
            
        with col2:
            excel_data = converter_para_excel(movs_df)
            st.download_button(
                label="📥 Baixar em Excel (.xlsx)", data=excel_data, 
                file_name=f"movimentacoes_{datetime.now().strftime('%Y%m%d')}.xlsx", 
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            )

# ═════════════════════════════════════════════════════════════
# CADASTRAR / EXCLUIR PRODUTOS
# ═════════════════════════════════════════════════════════════
with aba_cadastro:
    col1, col2 = st.columns(2)

    with col1:
        st.subheader("➕ Novo Produto")
        nome_novo = st.text_input("Nome do produto")
        estoque_minimo = st.number_input("Estoque mínimo", min_value=0, value=10)
        valor_unitario = st.number_input("Valor unitário (R$)", min_value=0.0, step=0.01, format="%.2f")

        if st.button("✅ Cadastrar", type="primary"):
            if not nome_novo.strip():
                st.error("Informe um nome para o produto.")
            else:
                ok, msg = cadastrar_produto(nome_novo.strip(), estoque_minimo, valor_unitario)
                if ok:
                    st.success(msg)
                    st.rerun()
                else:
                    st.error(msg)

    with col2:
        st.subheader("🗑️ Excluir Produto")
        produtos_df = listar_produtos()
        if not produtos_df.empty:
            opcoes_del = dict(zip(produtos_df["nome"], produtos_df["id"]))
            nome_del = st.selectbox("Selecione um produto", list(opcoes_del.keys()), key="del_prod")
            st.warning("Ao excluir um produto, todo o histórico de movimentação dele será apagado.")
            if st.button("🗑️ Confirmar Exclusão"):
                deletar_produto(opcoes_del[nome_del])
                st.success("Produto excluído com sucesso!")
                st.rerun()
        else:
            st.info("Nenhum produto cadastrado.")