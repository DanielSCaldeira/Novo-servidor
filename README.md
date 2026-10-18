# VPS Oracle Always Free

[![Última execução da Action](https://github.com/DanielSCaldeira/Novo-servidor/actions/workflows/oracle-vps.yml/badge.svg?branch=main)](https://github.com/DanielSCaldeira/Novo-servidor/actions/workflows/oracle-vps.yml)

**[Abrir execuções e resumo da Oracle](https://github.com/DanielSCaldeira/Novo-servidor/actions/workflows/oracle-vps.yml)** · **[Acompanhar avisos](https://github.com/DanielSCaldeira/Novo-servidor/issues/1)**

> [!WARNING]
> **Último erro de provisionamento confirmado em 17/10/2026: `Out of host capacity` — Oracle sem capacidade para criar a A1 naquele momento.**
> Este é um registro histórico. O resultado atualizado da Oracle aparece em destaque no **Summary / Resumo** de cada execução.

> [!NOTE]
> O badge acima atualiza automaticamente o resultado da Action. Um diagnóstico verde confirma que a consulta funcionou; a criação da VPS precisa ser confirmada separadamente no resultado da Oracle.

Automação para a stack existente **vps-ampere-a1-gratis**, na região **sa-saopaulo-1**.
As duas VPS AMD existentes não podem ser alteradas pelo plano aceito pelo controlador.

## Como funciona

- GitHub Actions consulta a stack manualmente ou a cada 10 minutos, nos minutos 7, 17, 27, 37, 47 e 57.
- Sem jobs paralelos: concurrency no GitHub e consulta dos jobs da stack na Oracle.
- Primeiro cria um **Plan** na mesma stack; em execução posterior valida o JSON e cria um **Apply daquele plano**.
- Não usa `AUTO_APPROVED`: aplicar o plano específico impede aceitar mudanças não verificadas.
- Cada execução consulta ou submete um job e termina. Não há espera pelo provisionamento no runner.
- O ciclo Plan/Apply ocupa execuções distintas: o intervalo entre Applies costuma ser de pelo menos 20 minutos.
- Só repete falhas reconhecidas exclusivamente como falta de capacidade. Falhas desconhecidas, autenticação, cota ou configuração pausam e desabilitam o workflow.
- Se a instância estiver RUNNING e o Apply tiver terminado com sucesso, registra o aviso na Issue e desabilita o workflow.
- Se a máquina existir após Apply parcialmente falho, pausa para revisão, sem criar outra.
- A Issue guarda apenas estado de controle, identificadores dos jobs e datas. Nenhum Terraform state ou credencial é publicado.

## Custo zero e configuração permitida

Runner **ubuntu-24.04 padrão**, repositório **público**, sem artifacts, caches ou runners maiores.
Repositórios públicos não consomem os 2.000 minutos mensais incluídos para repositórios privados do GitHub Free.
O controlador verifica a visibilidade via API antes de fazer operações na Oracle.

Na Oracle, aceita somente uma nova **VM.Standard.A1.Flex, 2 OCPUs, 12 GB**, chamada
**vps-ampere-a1**, com imagem oficial de **Oracle Linux 9**, boot volume de tamanho
**explicitamente definido, até 50 GB**, Balanced (10 VPUs/GB), na home region São Paulo.
Permite criar rede básica (VCN, subnet, Internet Gateway, rotas, security lists/NSGs e DHCP).
Recusa outros recursos gerenciados, provisioners, providers externos e qualquer update/delete/replacement.
Uma stack que também gerencie as AMD será bloqueada, inclusive se elas aparecerem como no-op.

Antes de aplicar, lê instâncias, boot volumes e block volumes de **todos os compartments**
na região, incluindo volumes sem anexos. Exige A1 livre na franquia conservadora de
2 OCPUs/12 GB, no máximo 200 GB combinados após a criação e cota de serviço disponível.
Os **150 GB anteriormente usados são uma referência, não uma medição atual**.
O tamanho da imagem também precisa caber no boot volume aprovado.

`ZERO_COST_CONFIRMED=true` é uma confirmação do operador, não uma consulta de faturamento.
Só ative após confirmar que a conta OCI permanece **Always Free, sem upgrade pago**, que
a stack e as cotas foram revisadas e que não há consumo pago habilitado.
O script não tem como garantir faturamento de uma conta Pay As You Go, nem controlar
alterações feitas por outros usuários ou consumo posterior das aplicações.
Não faça alterações manuais na stack enquanto as tentativas estiverem habilitadas.

Fontes: [GitHub Actions billing](https://docs.github.com/en/billing/concepts/product-billing/github-actions),
[Oracle Always Free](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm).

## Secrets e variáveis do GitHub

Em **Settings → Secrets and variables → Actions**, cadastrar:

| Secret | Conteúdo |
| --- | --- |
| `OCI_STACK_ID` | OCID da stack existente, informado abaixo |
| `OCI_TENANCY` | OCID da tenancy |
| `OCI_USER` | OCID do usuário de API |
| `OCI_FINGERPRINT` | Fingerprint da chave de API cadastrada na OCI |
| `OCI_PRIVATE_KEY` | PEM completo da chave privada de **API OCI** |
| `OCI_KEY_PASSPHRASE` | Opcional, senha da chave de API |

Não utilizar a chave SSH da VPS como chave de API. Não enviar chaves pelo chat nem incluí-las em commits.
A pasta local já contém um par SSH, excluído pelo `.gitignore`.

| Variable | Valor inicial | Quando alterar |
| --- | --- | --- |
| `RETRY_ENABLED` | `false` | `true` somente para a primeira tentativa revisada e o cron |
| `ZERO_COST_CONFIRMED` | `false` | `true` após conferir gratuidade, stack, cotas e permissões |
| `APPROVED_STACK_SHA256` | vazio | Hash do diagnóstico, após revisar os arquivos e variáveis da stack |

Não é necessário PAT no workflow: ele usa `GITHUB_TOKEN` com `contents: read`, `issues: write`,
`actions: write`. Não existem triggers de pull request ou push.

## IAM OCI

Criar um usuário/grupo dedicado para a chave de API. A stack precisa usar configuração
ZIP estática, com provider oficial `oracle/oci`; fontes Git/Object Storage dinâmicas e
providers personalizados são recusados.

Stacks existentes também podem registrar o namespace oficial legado `hashicorp/oci`,
conforme a [documentação de migração da Oracle](https://docs.oracle.com/en-us/iaas/Content/dev/terraform/migrating-legacy.htm).
O validador aceita exclusivamente os endereços completos desses dois providers no registry oficial;
não aceita outros hosts, namespaces ou providers. Isso evita bloquear o Plan da stack existente
sem alterar o Terraform, o state ou as verificações de recursos e custo.

O inventário exige leitura em toda a tenancy para não ignorar consumo de outro compartment.
Exemplo de política, substituindo grupo e compartment (revisar com o administrador OCI):

```text
Allow group github-vps-retry to inspect compartments in tenancy
Allow group github-vps-retry to inspect tenancies in tenancy
Allow group github-vps-retry to inspect limits in tenancy
Allow group github-vps-retry to read instances in tenancy
Allow group github-vps-retry to read instance-images in tenancy
Allow group github-vps-retry to read volumes in tenancy
Allow group github-vps-retry to read boot-volumes in tenancy
Allow group github-vps-retry to read orm-stacks in compartment <compartment>
Allow group github-vps-retry to manage orm-jobs in compartment <compartment>
```

O Apply executa com as permissões OCI do solicitante: também é necessário permitir os
recursos que a stack cria, no compartment pretendido. Se a stack já usa rede existente,
conceder apenas `use virtual-network-family` nela. Para criar VCN/subnet/rotas, precisará
das respectivas permissões de criação. `manage instance-family` e `manage volume-family`
no compartment alvo são exemplos funcionais, mas amplos: preferir permissões específicas
para criação e leitura, e limitar por compartment. Não conceder edição/destruição das AMD.

Como a forma exata depende da rede e do compartment reais da stack, a política final
deve ser validada no diagnóstico/primeiro Plan. Falta de acesso pausa, sem tentar contornar IAM.

## Ativação

**Ativação realizada em 18/10/2026:** tentativas habilitadas, com os limites de A1 2 OCPUs/12 GB e disco 50 GB/Balanced. O Plan real revelou provider OCI legado e disco indefinido; ambos foram tratados antes do Apply. Os campos de disco em string são normalizados conforme o schema OCI. O estado atualizado permanece na [Issue de acompanhamento](https://github.com/DanielSCaldeira/Novo-servidor/issues/1).

O input manual `configure_boot_disk` prepara **somente** os atributos de disco na configuração ZIP da stack aprovada. Exige inventário sem A1, até 150 GB usados, ausência de jobs ativos e ETag, e não cria recursos. Precisa de permissão para atualizar essa stack. Depois da edição, registrar o novo hash aprovado e retomar com `resume_blocked=true`; no uso normal manter esse input **false**. Não altera state nem arquivos fora do resource da instância. Expressões complexas ou configurações ambíguas são recusadas.

1. Cadastrar os Secrets e manter `RETRY_ENABLED=false`.
2. Em **Actions → Oracle Always Free VPS → Run workflow**, executar com `diagnostic=true`.
   É somente leitura, inclusive no GitHub: mostra o hash da configuração e contadores agregados.
3. Revisar na Oracle os arquivos Terraform, módulos, variáveis, região, imagem, tamanho do disco
   e recursos gerenciados. Conferir que o nome da stack e seu OCID são os pretendidos.
   O hash detecta alterações; **não é uma aprovação automática da segurança do código**.
4. Salvar o hash em `APPROVED_STACK_SHA256` e confirmar `ZERO_COST_CONFIRMED=true`.
5. Executar manualmente com `diagnostic=false`, `test_notification=true`.
   A Issue será criada pelo bot. Abrir a Issue, clicar **Subscribe** e habilitar notificações
   por e-mail em [Notification settings](https://github.com/settings/notifications).
   Executar o teste novamente após assinar: cada execução manual envia uma nova menção,
   sem repetir comentários se houver recuperação da mesma execução.
6. Após conferir o canal de aviso, configurar `RETRY_ENABLED=true` e executar manualmente
   com `diagnostic=false`, `test_notification=false`. Essa execução cria somente o primeiro Plan.
7. Conferir o Plan no console. A próxima execução valida todos os recursos antes de criar o Apply.
   Para acompanhar sem aguardar o cron, executar manualmente novamente após o job terminar.

A primeira tentativa real só deve ocorrer depois dessas verificações. O script nunca
cria/recria a stack, nunca executa Terraform local e nunca apaga volumes automaticamente.

## Pausar e retomar

- Pausa manual: `RETRY_ENABLED=false`. Um job já submetido à Oracle continua lá;
  a variável não o cancela. A próxima consulta manual pode confirmar sua situação.
- Bloqueio: ler a Issue e os detalhes privados do job no console OCI, corrigir a causa,
  revisar novamente qualquer alteração de Terraform e atualizar o hash aprovado.
- Reabilitar o workflow na aba Actions e executar manualmente com `diagnostic=false`,
  `resume_blocked=true` e `RETRY_ENABLED=true`. A retomada não ignora as validações.
  A retomada reconhece o erro terminal anterior e prepara um novo Plan após as correções;
  uma solicitação com resposta incerta mantém o token original para evitar duplicação.
  Máquina parcialmente criada continua bloqueando novos Applies. Não apagar a Issue para forçar criação.
- Sucesso é terminal: reabilitar o workflow não recria a VPS. A Issue permanece como registro.
- Se não for possível publicar o aviso/desativar, consultar o run com falha e executar
  manualmente outra vez. O estado persistido impede criar outra VPS ao repetir o aviso.

O GitHub pode atrasar/perder execuções agendadas. Em repositório público, pode desativar
o cron após 60 dias sem atividade; verificar a aba Actions e reativar se necessário.
Não é uma fila de reserva de capacidade nem garantia de disponibilidade.
[Limitações do cron](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows).

## Desenvolvimento e diagnóstico local

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
.\.venv\Scripts\python -m unittest discover -s tests -v
$env:OCI_STACK_ID = 'OCID_DA_STACK'
$env:OCI_PROFILE = 'DEFAULT'
.\.venv\Scripts\python scripts/tentar_vps.py
```

Execução local é sempre somente leitura, usando o perfil de API em `%USERPROFILE%\.oci\config`.
Não imprime configuração OCI, chave, plano completo ou Terraform state.
