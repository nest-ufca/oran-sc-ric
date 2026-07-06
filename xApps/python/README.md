# xApps Python do Testbed

Este diretorio guarda os xApps Python usados no testbed com o OSC RIC simplificado.
Eles ficam dentro do submodulo `oran-sc-ric` porque rodam no container
`python_xapp_runner` e importam diretamente a base `lib.xAppBase` fornecida por esse
repositorio.

## Visao geral

Os xApps atuais sao:

- `kpm_metrics_collector.py`: assina metricas E2SM-KPM no gNB e salva as amostras em CSV.
- `rc_slice_controller.py`: le as metricas KPM mais recentes do CSV e envia controles E2SM-RC para ajustar quotas de PRB por UE/slice.

O fluxo esperado e:

1. Subir o Near-RT RIC com `docker compose`.
2. Subir o gNB com E2 habilitado.
3. Atachar os UEs.
4. Rodar o xApp KPM para gerar o CSV de metricas.
5. Rodar o xApp RC usando o CSV gerado pelo KPM.

## KPM metrics collector

O arquivo `kpm_metrics_collector.py` cria uma subscription E2SM-KPM e registra as
metricas recebidas em um CSV. No cenario multi-UE, o uso principal e o Report Style
5, que permite coletar metricas de varios UEs em uma unica subscription.

O CSV inclui campos de contexto como timestamp, E2 node, subscription, report style,
UE ID, slice ID e periodo de granularidade, alem das metricas recebidas do KPM.

Exemplo:

```bash
cd /home/weskley/NEST_TESTBED_OCUDU/submodules/oran-sc-ric

docker compose exec python_xapp_runner ./kpm_metrics_collector.py \
  --e2_node_id=gnbd_999_070_00019b_0 \
  --ran_func_id=2 \
  --kpm_report_style=5 \
  --ue_ids=0,1,2,3,4,5,6,7 \
  --csv_path=/opt/xApps/data/kpm_metrics.csv
```

Metricas padrao coletadas:

```text
DRB.UEThpDl,CQI,RRU.PrbAvailDl,RRU.PrbUsedDl,RRU.PrbTotDl,DRB.RlcSduDelayDl,DRB.RlcPacketDropRateDl
```

Para mudar as metricas:

```bash
docker compose exec python_xapp_runner ./kpm_metrics_collector.py \
  --e2_node_id=gnbd_999_070_00019b_0 \
  --ran_func_id=2 \
  --kpm_report_style=5 \
  --ue_ids=0,1,2,3,4,5,6,7 \
  --metrics=DRB.UEThpDl,CQI,RRU.PrbUsedDl \
  --csv_path=/opt/xApps/data/kpm_metrics.csv
```

## RC slice controller

O arquivo `rc_slice_controller.py` usa o CSV gerado pelo KPM para tomar decisoes
simples de controle. Ele le a amostra mais recente de cada UE, associa cada UE a um
slice e envia comandos E2SM-RC para configurar quotas de PRB.

Exemplo controlando todos os UEs encontrados no CSV:

```bash
cd /home/weskley/NEST_TESTBED_OCUDU/submodules/oran-sc-ric

docker compose exec python_xapp_runner ./rc_slice_controller.py \
  --e2_node_id=gnbd_999_070_00019b_0 \
  --ran_func_id=3 \
  --csv_path=/opt/xApps/data/kpm_metrics.csv \
  --plmn=99970 \
  --sst=1 \
  --sd=1 \
  --control_all_ues
```

Exemplo controlando apenas um UE:

```bash
docker compose exec python_xapp_runner ./rc_slice_controller.py \
  --e2_node_id=gnbd_999_070_00019b_0 \
  --ran_func_id=3 \
  --gnb_cu_ue_f1ap_id=2 \
  --csv_path=/opt/xApps/data/kpm_metrics.csv \
  --plmn=99970 \
  --sst=1 \
  --sd=1
```

O xApp RC espera metricas recentes no CSV. Por isso, rode o KPM collector antes do
RC controller e mantenha os dois apontando para o mesmo `--csv_path`.

## Dados gerados

Por padrao, os CSVs sao escritos dentro do container em:

```text
/opt/xApps/data/kpm_metrics.csv
```

No repositorio, a pasta `xApps/python/data/` e tratada como saida de execucao e fica
fora do Git. Assim os resultados dos experimentos nao poluem os commits do codigo.

## TODO

- Tornar o mapa UE -> slice configuravel por arquivo YAML ou JSON.
- Tornar a politica de slices configuravel fora do codigo.
- Separar claramente modo de observacao, modo dry-run e modo de controle efetivo.
- Documentar quais metricas KPM sao obrigatorias para cada politica RC.
- Melhorar logs e tratamento de erro quando o CSV ainda nao existe ou esta sem metricas recentes.
- Salvar resultados de experimentos em uma pasta externa padronizada para analise posterior.
- Evoluir a politica RC para usar metricas como throughput, CQI, PRB usado, atraso e perda por slice.