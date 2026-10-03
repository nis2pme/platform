"""As duas maneiras de chegar a esta versão passam a dar a mesma base.

Uma instalação nova cria as tabelas a partir dos modelos e depois corre a cadeia
toda; as migrações que criam tabelas guardam-se com "se ainda não existir" e
saltam o trabalho por encontrarem tudo feito. Quem atualiza faz o percurso
inverso: as tabelas nascem das migrações e os modelos nunca lhes tocam. Onde as
duas descrições discordavam, metade do parque ficou com uma base diferente da
outra metade — e nada acusava, porque as duas funcionam.

O que se corrige aqui, e o que cada uma custava:

  - **`evidencia_requisito`**: faltava o índice único parcial nas instalações
    novas. `ligar()` consulta-e-insere e conta com ele para recusar a segunda
    linha; sem ele, dois pedidos simultâneos deixavam duas ligações ativas para
    o mesmo par, em silêncio.
  - **`controlo_estado_historico.origem`**: era um tipo enumerado do Postgres
    nas instalações novas. A decisão escrita é o contrário — texto, para que
    acrescentar uma origem nova não obrigue a uma migração de tipo. Onde nasceu
    enumerado, acrescentar um valor rebentava.
  - **`notificacoes.chave_dedup`**: ficou com 100 caracteres em quem atualizou
    (a coluna foi renomeada, nunca alargada) e 255 em quem instalou de novo. As
    chaves de hoje chegam aos 90 — dez de folga, e o modelo diz 255. Faltava
    também o índice, que quem atualizou nunca chegou a ter porque a coluna
    antiga não o tinha para herdar.
  - **Instantes sem fuso** em seis colunas nas instalações novas. Não parte
    nada — o código repõe o fuso ao ler — mas a mesma data sai escrita de duas
    maneiras no dossiê, que é peça de auditoria.
  - **`server_default` esquecidos** de migrações que os usaram para preencher
    linhas antigas e não os retiraram. O valor vem sempre do ORM.
  - **Índices** que só existiam de um dos lados.

Tudo guardado: numa instalação nova, onde os modelos já produzem a forma certa,
esta migração não faz nada. Sem downgrade — é convergência, e reverter seria
reintroduzir a divergência de propósito.

Revision ID: 027_convergencia_esquema
Revises: 026_evidencia_lapide_rgpd
Create Date: 2026-09-09
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "027_convergencia_esquema"
down_revision: Union[str, None] = "026_evidencia_lapide_rgpd"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# (tabela, coluna) cujo `server_default` sobrou de uma migração de preenchimento.
_DEFAULTS_A_LIMPAR = (
    ("notificacoes", "acionavel"),
    ("notificacoes", "categoria"),
    ("notificacoes", "severidade"),
    ("evidencias", "eliminacao_rgpd"),
    ("evidencia_requisito", "ambito_por_confirmar"),
    ("utilizadores", "tentativas_falhadas"),
    ("login_bloqueios_ip", "contador"),
    ("controlo_estado_historico", "origem"),
)

# Instantes que têm de guardar o fuso: a app trabalha em UTC.
_COM_FUSO = (
    ("utilizadores", "bloqueado_ate", True),
    ("utilizadores", "tentativas_janela_inicio", True),
    ("login_bloqueios_ip", "janela_inicio", False),
    ("login_bloqueios_ip", "bloqueado_ate", True),
    ("login_bloqueios_ip", "atualizado_em", False),
    ("controlos_empresa_v2", "na_definido_em", True),
)


def _colunas(inspector, tabela: str) -> dict:
    return {c["name"]: c for c in inspector.get_columns(tabela)}


def _indices(inspector, tabela: str) -> set:
    return {i["name"] for i in inspector.get_indexes(tabela)}


def _tabelas(inspector) -> set:
    return set(inspector.get_table_names())


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    postgres = bind.dialect.name == "postgresql"
    tabelas = _tabelas(inspector)

    # ── 1. A ligação ativa é única por par ───────────────────────────────────
    if "evidencia_requisito" in tabelas:
        if "uq_evidencia_requisito_ativa" not in _indices(inspector, "evidencia_requisito"):
            op.create_index(
                "uq_evidencia_requisito_ativa",
                "evidencia_requisito",
                ["evidencia_id", "requisito_id"],
                unique=True,
                postgresql_where=sa.text("desligado_em IS NULL"),
                sqlite_where=sa.text("desligado_em IS NULL"),
            )

    # ── 2. A origem da transição é texto, não um tipo enumerado ──────────────
    if postgres and "controlo_estado_historico" in tabelas:
        tipo_atual = _colunas(inspector, "controlo_estado_historico")["origem"]["type"]
        if not isinstance(tipo_atual, sa.String):
            # `USING` porque o Postgres não converte um enumerado em texto
            # sozinho. O valor é o mesmo — muda o tipo que o guarda.
            op.execute(
                "ALTER TABLE controlo_estado_historico "
                "ALTER COLUMN origem TYPE VARCHAR(20) USING origem::text"
            )
            op.execute("DROP TYPE IF EXISTS origemtransicao")

        # O JSON das evidências não tem limite de tamanho útil.
        if _colunas(inspector, "controlo_estado_historico")["evidencias"]["type"].__class__ \
                is not sa.Text:
            op.execute(
                "ALTER TABLE controlo_estado_historico "
                "ALTER COLUMN evidencias TYPE TEXT"
            )

    # ── 3. Índices que só existiam de um dos lados ───────────────────────────
    if "controlo_estado_historico" in tabelas:
        idx = _indices(inspector, "controlo_estado_historico")
        if "ix_controlo_estado_historico_controlo_em" not in idx:
            op.create_index(
                "ix_controlo_estado_historico_controlo_em",
                "controlo_estado_historico",
                ["controlo_empresa_id", "ocorrido_em"],
            )
        # O de uma coluna só é o prefixo do composto: peso de escrita sem
        # leitura que o use.
        if "ix_controlo_estado_historico_controlo_empresa_id" in idx:
            op.drop_index(
                "ix_controlo_estado_historico_controlo_empresa_id",
                table_name="controlo_estado_historico",
            )

    if "utilizador_empresa" in tabelas:
        if "ix_utilizador_empresa_empresa_id" not in _indices(inspector, "utilizador_empresa"):
            op.create_index(
                "ix_utilizador_empresa_empresa_id", "utilizador_empresa", ["empresa_id"]
            )

    # ── 4. A chave de deduplicação das notificações ──────────────────────────
    if "notificacoes" in tabelas:
        coluna = _colunas(inspector, "notificacoes").get("chave_dedup")
        if coluna is not None and getattr(coluna["type"], "length", None) != 255:
            op.alter_column(
                "notificacoes",
                "chave_dedup",
                existing_type=sa.String(length=100),
                type_=sa.String(length=255),
                existing_nullable=False,
            )
        if "ix_notificacoes_chave_dedup" not in _indices(inspector, "notificacoes"):
            op.create_index("ix_notificacoes_chave_dedup", "notificacoes", ["chave_dedup"])

    # ── 5. Instantes com fuso ────────────────────────────────────────────────
    if postgres:
        for tabela, coluna, anulavel in _COM_FUSO:
            if tabela not in tabelas:
                continue
            info = _colunas(inspector, tabela).get(coluna)
            if info is None or getattr(info["type"], "timezone", False):
                continue
            # Os valores já lá estão em UTC: o `AT TIME ZONE 'UTC'` diz isso ao
            # Postgres em vez de os deslocar pelo fuso da sessão.
            op.execute(
                f"ALTER TABLE {tabela} ALTER COLUMN {coluna} "
                f"TYPE TIMESTAMP WITH TIME ZONE USING {coluna} AT TIME ZONE 'UTC'"
            )

    # ── 6. `server_default` que sobraram de migrações de preenchimento ───────
    for tabela, coluna in _DEFAULTS_A_LIMPAR:
        if tabela not in tabelas:
            continue
        info = _colunas(inspector, tabela).get(coluna)
        if info is not None and info.get("default") is not None:
            op.alter_column(tabela, coluna, server_default=None)

    # ── 7. Auto-reparação: a chave entre as tarefas e as suas conclusões ─────
    # Uma versão desta cadeia adiava chaves estrangeiras para tabelas que ainda
    # não existissem e, por a fotografia ser tirada antes do ciclo, adiava
    # também a que aponta para uma tabela criada nesse mesmo ciclo. Ninguém a
    # repunha, porque o destino não vem à frente — vem ao lado.
    if {"tarefas", "tarefa_conclusoes"} <= tabelas:
        tem_fk = any(
            "tarefa_id" in (fk.get("constrained_columns") or [])
            for fk in inspector.get_foreign_keys("tarefa_conclusoes")
        )
        if not tem_fk:
            op.create_foreign_key(
                "fk_tarefa_conclusoes_tarefa_id",
                "tarefa_conclusoes", "tarefas", ["tarefa_id"], ["id"],
            )


def downgrade() -> None:
    """Sem reversão, por regra permanente.

    Isto é convergência: reverter seria devolver às bases a divergência que a
    migração existe para apagar.
    """
