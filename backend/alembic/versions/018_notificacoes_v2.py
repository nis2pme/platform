"""Centro de notificações: conteúdo estruturado, alvo genérico e retenção.

Até aqui uma notificação era uma frase pronta, escrita em português dentro do
backend, e um campo `tipo` que acumulava dois papéis incompatíveis: dizer a que
categoria o aviso pertence e servir de chave de deduplicação. Como a chave leva
o identificador da entidade lá dentro (`incidente_prazo:<uuid>:<marco>`), não
havia maneira de filtrar ou agrupar por categoria em SQL.

Esta migração separa as duas coisas e acrescenta o que faltava para haver uma
página de histórico a sério:

  - `codigo` + `params` — o que aconteceu e os valores que entram na frase. O
    texto passa a ser composto no idioma de quem lê, em vez de ficar gravado.
  - `categoria`, `severidade` — filtros e ordenação da atenção.
  - `entidade_tipo` + `entidade_id` — destino do link. Os avisos de prazo de
    incidente e de tarefa não tinham nenhum e obrigavam a procurar à mão.
  - `acionavel` — separa o que descreve trabalho por fazer do que é apenas
    informação; só o segundo se dispensa por se visitar o ecrã.
  - `lida_at` — quando foi lida, sem o que não há retenção possível.

`tipo` passa a chamar-se `chave_dedup`, que é o único papel que lhe fica.

`titulo` e `mensagem` passam a nuláveis: as linhas novas não os escrevem. As
antigas ficam exatamente como estão e continuam a ser mostradas a partir daí —
sem `params`, nunca poderiam ser recompostas noutro idioma, e inventar-lhes um
código não as tornaria legíveis. Por isso o backfill preenche o que dá para
saber com rigor (categoria, severidade, entidade, se é acionável) e deixa
`codigo` a nulo.

Estritamente idempotente. Em bases frescas o create_all da 001 já cria a tabela
com esta forma, porque ela vem do modelo; cada passo é condicionado ao que o
inspetor encontrar. Aditiva — sem downgrade, por regra permanente.

Revision ID: 018_notificacoes_v2
Revises: 017_politica_origem
Create Date: 2026-08-03
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "018_notificacoes_v2"
down_revision: Union[str, None] = "017_politica_origem"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Prefixo do `tipo` antigo → o que se consegue afirmar sobre essas linhas.
# (categoria, severidade, entidade_tipo, acionavel, id_vem_da_chave)
_LEGADO = (
    ("incidente_prazo:", "incidentes", "aviso", "Incidente", True, True),
    ("tarefa_prazo:", "tarefas", "aviso", "Tarefa", True, True),
    ("controlo_decisao_auditoria", "controlos", "info", "ControloEmpresaV2", False, False),
    ("parecer_pedido", "auditoria", "aviso", "ControloEmpresaV2", False, False),
    ("conetor_drift", "conetores", "aviso", "ControloEmpresaV2", False, False),
    ("conetor_contradicao", "conetores", "critico", "ControloEmpresaV2", False, False),
)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)

    if "notificacoes" not in inspector.get_table_names():
        # Base sem a tabela: o create_all da 001 cria-a já com esta forma.
        return

    colunas = {c["name"] for c in inspector.get_columns("notificacoes")}
    indices = {i["name"] for i in inspector.get_indexes("notificacoes")}

    # --- 1. `tipo` passa a dizer o que faz ---------------------------------
    if "tipo" in colunas and "chave_dedup" not in colunas:
        op.alter_column("notificacoes", "tipo", new_column_name="chave_dedup")
        colunas.discard("tipo")
        colunas.add("chave_dedup")
        # O índice acompanha a coluna mas mantém o nome antigo; alinhá-lo evita
        # que uma base atualizada e uma base fresca fiquem com nomes diferentes.
        if bind.dialect.name == "postgresql" and "ix_notificacoes_tipo" in indices:
            op.execute(
                "ALTER INDEX ix_notificacoes_tipo "
                "RENAME TO ix_notificacoes_chave_dedup"
            )
            indices.discard("ix_notificacoes_tipo")
            indices.add("ix_notificacoes_chave_dedup")

    # --- 2. Colunas novas ---------------------------------------------------
    novas = (
        ("codigo", sa.Column("codigo", sa.String(length=100), nullable=True)),
        ("categoria", sa.Column(
            "categoria", sa.String(length=40), nullable=False,
            server_default="sistema",
        )),
        ("severidade", sa.Column(
            "severidade", sa.String(length=20), nullable=False,
            server_default="info",
        )),
        ("params", sa.Column("params", sa.String(), nullable=True)),
        ("entidade_tipo", sa.Column(
            "entidade_tipo", sa.String(length=50), nullable=True,
        )),
        ("entidade_id", sa.Column("entidade_id", sa.Uuid(), nullable=True)),
        ("acionavel", sa.Column(
            "acionavel", sa.Boolean(), nullable=False, server_default=sa.false(),
        )),
        # Sem fuso, como o `created_at` que já lá estava — duas colunas de tempo
        # na mesma tabela com convenções diferentes seria uma armadilha.
        ("lida_at", sa.Column("lida_at", sa.DateTime(), nullable=True)),
    )
    for nome, coluna in novas:
        if nome not in colunas:
            op.add_column("notificacoes", coluna)

    # --- 3. O texto deixa de ser obrigatório --------------------------------
    # As linhas novas guardam `codigo` + `params` e não escrevem frase nenhuma.
    for nome, tipo in (("titulo", sa.String(length=255)), ("mensagem", sa.Text())):
        op.alter_column(
            "notificacoes", nome, existing_type=tipo, nullable=True,
        )

    # --- 4. Backfill ---------------------------------------------------------
    # Só em Postgres: é o único motor onde existem instalações com histórico.
    # Uma base SQLite aqui é sempre uma base de teste acabada de criar, sem
    # linhas nenhumas para converter.
    if bind.dialect.name == "postgresql":
        _backfill(bind)

    # --- 5. Índices ----------------------------------------------------------
    novos_indices = (
        ("ix_notif_utilizador_lida_data", ["utilizador_id", "lida", "created_at"]),
        ("ix_notif_utilizador_categoria", ["utilizador_id", "categoria"]),
        ("ix_notif_lida_lida_at", ["lida", "lida_at"]),
        ("ix_notificacoes_codigo", ["codigo"]),
    )
    for nome, cols in novos_indices:
        if nome not in indices:
            op.create_index(nome, "notificacoes", cols)

    # Redundantes a partir daqui: `utilizador_id` e `created_at` são servidos
    # pelo índice composto, e `ix_notificacoes_id` duplica a chave primária —
    # o mesmo btree mantido duas vezes em cada inserção.
    for nome in (
        "ix_notificacoes_id",
        "ix_notificacoes_utilizador_id",
        "ix_notificacoes_created_at",
    ):
        if nome in indices:
            op.drop_index(nome, table_name="notificacoes")


def _backfill(bind) -> None:
    """Deriva o que se consegue saber com rigor a partir da chave antiga."""
    for prefixo, categoria, severidade, entidade, acionavel, id_da_chave in _LEGADO:
        # `like` para as chaves que continuam com o identificador atrás;
        # igualdade para as que são o tipo inteiro.
        padrao = f"{prefixo}%" if prefixo.endswith(":") else prefixo
        operador = "LIKE" if prefixo.endswith(":") else "="

        bind.execute(
            sa.text(
                f"""
                UPDATE notificacoes
                   SET categoria = :categoria,
                       severidade = :severidade,
                       entidade_tipo = :entidade,
                       acionavel = :acionavel
                 WHERE chave_dedup {operador} :padrao
                   AND codigo IS NULL
                """
            ),
            {
                "categoria": categoria,
                "severidade": severidade,
                "entidade": entidade,
                "acionavel": acionavel,
                "padrao": padrao,
            },
        )

        if id_da_chave:
            # O identificador está na segunda posição da chave. A expressão
            # regular no WHERE garante que só se converte o que é mesmo um
            # identificador — uma chave malformada fica sem alvo em vez de
            # fazer a migração inteira falhar.
            bind.execute(
                sa.text(
                    """
                    UPDATE notificacoes
                       SET entidade_id = split_part(chave_dedup, ':', 2)::uuid
                     WHERE chave_dedup ~ :regex
                       AND entidade_id IS NULL
                    """
                ),
                {
                    "regex": (
                        f"^{prefixo.rstrip(':')}:"
                        r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
                        r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}(:|$)"
                    )
                },
            )
        else:
            # Estas já apontavam para um controlo pela chave estrangeira.
            bind.execute(
                sa.text(
                    """
                    UPDATE notificacoes
                       SET entidade_id = controlo_empresa_id
                     WHERE chave_dedup = :padrao
                       AND controlo_empresa_id IS NOT NULL
                       AND entidade_id IS NULL
                    """
                ),
                {"padrao": padrao},
            )


def downgrade() -> None:
    # Aditiva por regra permanente — sem downgrade.
    pass
