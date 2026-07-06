#!/usr/bin/env python3

import argparse
import signal
from lib.xAppBase import xAppBase
# Adicionando bibliotecas para expotar do csv
import csv   # Biblioteca padrão do Python para ler/escrever arquivos CSV.
import os    # Usada para criar diretórios e verificar se o arquivo existe.
import time  # Usada para salvar o timestamp Unix de quando a métrica chegou

UE_TO_SLICE = {
    0: "SST_1_eMBB",
    1: "SST_1_eMBB",
    2: "SST_1_eMBB",
    3: "SST_2_URLLC",
    4: "SST_2_URLLC",
    5: "SST_2_URLLC",
    6: "SST_3_mMTC",
    7: "SST_3_mMTC",
}


class MyXapp(xAppBase):
    # Chama o construtor da classe base xAppBase.
    # Isso inicializa a comunicação do xApp com o RIC, RMR, HTTP server etc.
    def __init__(self, config, http_server_port, rmr_port, csv_path):
        super(MyXapp, self).__init__(config, http_server_port, rmr_port)
        # Caminho onde o arquivo CSV será salvo. # Exemplo: /opt/xApps/Metrics/kpm_metrics.csv
        self.csv_path = csv_path
        # Cabeçalho do CSV. # Começa como None porque ainda não sabemos quais métricas vão chegar.
        self.csv_header = None

        # Se existir um diretório no caminho, cria ele caso ainda não exista.
        csv_dir = os.path.dirname(self.csv_path)
        if csv_dir:
            os.makedirs(csv_dir, exist_ok=True)

    def _to_scalar(self, value):
        # Algumas métricas chegam como lista com um único valor.# Exemplo: [123.0]
        # Para o CSV ficar mais limpo, convertemos [123.0] em 123.0.
        if isinstance(value, list) and len(value) == 1:
            return value[0]

        # Se não for lista de um elemento, retorna o valor original.
        return value

    def write_csv(self, e2_agent_id, subscription_id, indication_hdr, meas_data, kpm_report_style, ue_id=None):
        # Pega o dicionário com as métricas recebidas no KPM.
        # Exemplo:
        # {
        #   "DRB.UEThpDl": [100.0],
        #   "DRB.UEThpUl": [50.0]
        # }
        metrics = meas_data["measData"]

        # Cria a linha base do CSV com informações gerais da indicação.
        row = {
            # Timestamp Unix do momento em que o xApp recebeu/processou a indicação.
            "timestamp_unix": time.time(),

            # Timestamp informado no cabeçalho da mensagem KPM.
            # O nome vem como "colletStartTime" na lib usada pelo xApp.
            "collect_start_time": indication_hdr.get("colletStartTime", ""),

            # ID do E2 node/gNB que enviou a indicação.
            "e2_agent_id": e2_agent_id,

            # ID da subscription criada pelo xApp.
            "subscription_id": subscription_id,

            # Report Style usado, por exemplo 1, 2 ou 5.
            "kpm_report_style": kpm_report_style,

            # UE ID.
            # No Style 1 ainda não temos UE específico, então fica vazio.
            "ue_id": "" if ue_id is None else ue_id,

            # Slice ID.
            # podemos preencher com UE -> slice.
            "slice_id": "" if ue_id is None else UE_TO_SLICE.get(ue_id, "unknown"),

            # Período de granularidade da medição.
            "granul_period": meas_data.get("granulPeriod", "")
        }

        # Adiciona cada métrica recebida como uma coluna da linha.
        for metric_name, value in metrics.items():
            # Exemplo:
            # metric_name = "DRB.UEThpDl"
            # value = [100.0]
            # row["DRB.UEThpDl"] = 100.0
            row[metric_name] = self._to_scalar(value)

        # Se o cabeçalho ainda não foi criado, cria usando as chaves da primeira linha.
        if self.csv_header is None:
            self.csv_header = list(row.keys())

        # Verifica se o arquivo já existe e se já tem conteúdo.
        # Se não existir ou estiver vazio, vamos escrever o cabeçalho.
        file_exists = os.path.exists(self.csv_path) and os.path.getsize(self.csv_path) > 0

        # Abre o CSV em modo append, ou seja, adiciona linhas sem apagar o conteúdo antigo.
        with open(self.csv_path, mode="a", newline="") as file:
            # DictWriter permite escrever uma linha a partir de um dicionário.
            writer = csv.DictWriter(file, fieldnames=self.csv_header)

            # Se o arquivo ainda não existia, escreve a primeira linha com o nome das colunas.
            if not file_exists:
                writer.writeheader()

            # Escreve a linha de métricas no CSV.
            writer.writerow(row)

    # Esta função é chamada quando o xApp recebe uma RIC Indication com métricas KPM. Ela serve tanto para Style 1, Style 2 e Style 5.
    def my_subscription_callback(self, e2_agent_id, subscription_id, indication_hdr, indication_msg, kpm_report_style=None, ue_id=None):
        # Se o kpm_report_style não foi passado pela callback/lambda, tenta pegar o valor salvo no objeto self.  Se também não existir, usa 1 como padrão.
        if kpm_report_style is None:
            kpm_report_style = getattr(self, "kpm_report_style", 1)
        
        # Prints gerais sobre a indication recebida.
        print("\nRIC Indication Received from {} for Subscription ID: {}".format(e2_agent_id, subscription_id))
        
        # Decodifica o cabeçalho da indicação KPM.  O cabeçalho contém informações como o horário de início da coleta.
        indication_hdr = self.e2sm_kpm.extract_hdr_info(indication_hdr)
         
        # Decodifica a mensagem KPM. Aqui ficam as métricas propriamente ditas, como throughput, CQI, PRB etc.
        meas_data = self.e2sm_kpm.extract_meas_data(indication_msg)
        print("[DEBUG] meas_data =", meas_data, flush=True)

        # Mostra o timestamp de início da coleta informado no header KPM.
        print("E2SM_KPM RIC Indication Content:")

        # Style 1 e Style 2 têm estrutura parecida: 
        # - Style 1: métricas agregadas do nó/célula
        # - Style 2: métricas de um UE específico    
        # # Nos dois casos as métricas vêm em meas_data["measData"].
        if kpm_report_style in [1, 2]:
            # Salva uma linha no CSV. No Style 1, ue_id normalmente é None. No Style 2, ue_id contém o UE monitorado.
            self.write_csv(e2_agent_id, subscription_id, indication_hdr, meas_data, kpm_report_style, ue_id)

            # Imprime as métricas no terminal.
            print("-Measurements Data:")

            # Pega o período de granularidade, se existir.
            granulPeriod = meas_data.get("granulPeriod", None)
            if granulPeriod is not None:
                print("-granulPeriod: {}".format(granulPeriod))

            # Percorre e imprime cada métrica recebida.  Exemplo: DRB.UEThpDl, CQI, RRU.PrbUsedDl etc.
            for metric_name, value in meas_data["measData"].items():
                print("--Metric: {}, Value: {}".format(metric_name, value))

         # Style 5 é usado para múltiplos UEs na mesma subscription.  A estrutura é diferente: as métricas vêm agrupadas em meas_data["ueMeasData"].
        elif kpm_report_style == 5:
            print("-UE Measurements Data:")
            # Percorre cada UE presente na indicação.
            # ueMeasData é esperado como um dicionário:
            # {
            #   "0": {"measData": {...}},
            #   "1": {"measData": {...}},
            #   ...
            # }
            for ue_id, ue_meas_data in meas_data["ueMeasData"].items():
                # Converte o UE ID para inteiro para bater com o mapa UE_TO_SLICE.
                ue_id = int(ue_id)

                 #Salva no CSV uma linha para este UE. Assim cada UE vira uma linha separada no dataset.
                self.write_csv(e2_agent_id, subscription_id, indication_hdr, ue_meas_data, kpm_report_style, ue_id)

                # Imprime qual UE está sendo mostrado.
                print("--UE ID: {}".format(ue_id))

                # Pega o período de granularidade desse UE, se existir.
                granulPeriod = ue_meas_data.get("granulPeriod", None)
                if granulPeriod is not None:
                    print("---granulPeriod: {}".format(granulPeriod))

                # Imprime as métricas desse UE específico.
                for metric_name, value in ue_meas_data["measData"].items():
                    print("---Metric: {}, Value: {}".format(metric_name, value))

    # Mark the function as xApp start function using xAppBase.start_function decorator.
    # It is required to start the internal msg receive loop.
    @xAppBase.start_function
    def start(self, e2_node_id, metric_names, kpm_report_style, ue_ids):
        # Print de debug indicando que o xApp iniciou. O flush=True força a mensagem a aparecer imediatamente no terminal/log.
        print("[KPM] xApp iniciado, aguardando RMR estabilizar...", flush=True)

        # Pequena espera para dar tempo do RMR/framework estabilizar antes de criar a subscription. Isso ajuda a reduzir comportamento intermitente do RIC/RMR.
        time.sleep(5)

        self.kpm_report_style = kpm_report_style
        # Período da subscription KPM. Aqui o xApp pede relatórios a cada 1000 ms.
        report_period = 1000
        
        # Granularidade da medição KPM. Aqui indica o período usado na medição/report de métricas.
        granul_period = 1000

        # Report Style 1: coleta métricas agregadas do E2 node/gNB. Não é específico de um UE.
        if (kpm_report_style == 1):
            # Mostra no terminal qual E2 node, report style e métricas serão assinadas.
            print("Subscribe to E2 node ID: {}, RAN func: e2sm_kpm, Report Style: {}, metrics: {}".format(e2_node_id, kpm_report_style, metric_names), flush=True)
            
            # Cria uma função callback intermediária.
             # O framework chama callbacks com 4 argumentos: agent, sub, hdr, msg.
            # Mas nossa função my_subscription_callback também precisa saber o report style. Como Style 1 não é por UE, passamos ue_id=None.
            callback = lambda agent, sub, hdr, msg: self.my_subscription_callback(agent, sub, hdr, msg, kpm_report_style, None)

             # Cria a subscription KPM Style 1. A partir daqui, quando a gNB enviar RIC Indication, a callback será chamada.
            self.e2sm_kpm.subscribe_report_service_style_1(e2_node_id, report_period, metric_names, granul_period, self.my_subscription_callback)
        
        # Report Style 2: coleta métricas de um UE específico.
        elif kpm_report_style == 2:
            #Para Style 2, usamos apenas o primeiro UE informado na lista.  Exemplo: --ue_ids=1 -> ue_ids = [1] -> ue_id = 1
            ue_id = ue_ids[0]
            
            # Mostra no terminal qual UE específico será monitorado.
            print("Subscribe to E2 node ID: {}, RAN func: e2sm_kpm, Report Style: {}, UE ID: {}, metrics: {}".format(e2_node_id, kpm_report_style, ue_id, metric_names), flush=True)

            # Cria uma callback intermediária passando também o UE ID.  Assim, quando a métrica chegar, conseguimos salvar ue_id e slice_id no CSV.
            callback = lambda agent, sub, hdr, msg: self.my_subscription_callback(agent, sub, hdr, msg, kpm_report_style, ue_id)

             # Cria a subscription KPM Style 2 para um UE específico.
            self.e2sm_kpm.subscribe_report_service_style_2(e2_node_id,report_period,ue_id,metric_names,granul_period,callback)

        # Report Style 5: coleta métricas de vários UEs
        elif kpm_report_style == 5:
            #Para Style 5, retornamos diversos UEs
            print("Subscribe to E2 node ID: {}, RAN func: e2sm_kpm, Report Style: {}, UE IDs: {}, metrics: {}".format(e2_node_id, kpm_report_style, ue_ids, metric_names), flush=True)

            # Cria uma callback intermediária passando também o UE ID.  Assim, quando a métrica chegar, conseguimos salvar ue_id e slice_id no CSV.
            callback = lambda agent, sub, hdr, msg: self.my_subscription_callback(agent, sub, hdr, msg, kpm_report_style, None)

            self.e2sm_kpm.subscribe_report_service_style_5(e2_node_id,report_period,ue_ids,metric_names,granul_period,callback)
        # Caso o usuário peça um report style que ainda não implementamos.
        else:
            print("Report Style {} ainda nao implementado".format(kpm_report_style), flush=True)



if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='My example xApp')
    parser.add_argument("--config", type=str, default='', help="xApp config file path")
    parser.add_argument("--http_server_port", type=int, default=8091, help="HTTP server listen port")
    parser.add_argument("--rmr_port", type=int, default=4561, help="RMR port")
    parser.add_argument("--e2_node_id", type=str, default='gnbd_999_070_00019b_0', help="E2 Node ID")
    parser.add_argument("--ran_func_id", type=int, default=2, help="RAN function ID")
    parser.add_argument("--kpm_report_style", type=int, default=1, help="xApp config file path") # Weskley: Adicionando report styles diferentes
    parser.add_argument("--ue_ids", type=str, default="0", help="UE IDs as comma-separated string")  #ADicionando ids dos UEs
    parser.add_argument("--csv_path",type=str, default="/opt/xApps/data/kpm_metrics.csv", help="CSV output path") # Caminho onde o CSV será salvo dentro do container do xApp.
    parser.add_argument("--metrics", type=str, default="DRB.UEThpDl,CQI,RRU.PrbAvailDl,RRU.PrbUsedDl,RRU.PrbTotDl,DRB.RlcSduDelayDl,DRB.RlcPacketDropRateDl", help="Metrics name as comma-separated string")

    args = parser.parse_args()
    config = args.config
    e2_node_id = args.e2_node_id # TODO: get available E2 nodes from SubMgr, now the id has to be given.
    ran_func_id = args.ran_func_id # TODO: get available E2 nodes from SubMgr, now the id has to be given.
    metrics_names = args.metrics.split(",")
    kpm_report_style = args.kpm_report_style # Weskley: Adicionando report styles diferentes
    ue_ids = list(map(int, args.ue_ids.split(","))) #Adicionando ids dos UEs


    # Cria o xApp passando também o caminho do CSV.
    myXapp = MyXapp(config, args.http_server_port, args.rmr_port, args.csv_path)
    myXapp.e2sm_kpm.set_ran_func_id(ran_func_id)

    # Connect exit signals.
    signal.signal(signal.SIGQUIT, myXapp.signal_handler)
    signal.signal(signal.SIGTERM, myXapp.signal_handler)
    signal.signal(signal.SIGINT, myXapp.signal_handler)

    # Start xApp.
    myXapp.start(e2_node_id, metrics_names, kpm_report_style, ue_ids)
    # Note: xApp will unsubscribe all active subscriptions at exit.
