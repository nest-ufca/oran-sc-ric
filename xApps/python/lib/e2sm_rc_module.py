import datetime
from enum import Enum
from .asn1.e2sm_rc_packer import e2sm_rc_packer
from .utils import plmn_string_to_bcd, plmn_to_bytes
from .asn1.nr_cgi_packer import nr_cgi_packer

class e2sm_rc_module(object):
    def __init__(self, parent):
        super(e2sm_rc_module, self).__init__()
        self.parent = parent
        self.ran_func_id = 3;
        self.e2sm_rc_compiler = e2sm_rc_packer()

        # helper variables
        self.requestorID = 0

    def set_ran_func_id(self, ran_func_id):
        self.ran_func_id = ran_func_id

    def get_requestor_id(self):
        self.requestorID += 1
        self.requestorID %= 255
        return self.requestorID

    @staticmethod
    def _encode_aper_length(length):
        """Encode one unfragmented APER length determinant."""
        if isinstance(length, bool) or not isinstance(length, int) or not 0 <= length <= 16383:
            raise ValueError("APER length must be an integer in the range 0..16383")

        if length < 128:
            return [length]

        return [0x80 | (length >> 8), length & 0xff]

    def _build_ric_control_request(self, control_header, control_msg, ack_request, requestor_id=None, instance_id=0):
        if requestor_id is None:
            requestor_id = self.get_requestor_id()

        ran_func_id = self.ran_func_id

        if isinstance(requestor_id, bool) or not isinstance(requestor_id, int) or not 0 <= requestor_id <= 65535:
            raise ValueError("RIC requestor ID must be an integer in the range 0..65535")

        if isinstance(instance_id, bool) or not isinstance(instance_id, int) or not 0 <= instance_id <= 65535:
            raise ValueError("RIC instance ID must be an integer in the range 0..65535")

        if isinstance(ran_func_id, bool) or not isinstance(ran_func_id, int) or not 0 <= ran_func_id <= 4095:
            raise ValueError("RAN Function ID must be an integer in the range 0..4095")

        if isinstance(ack_request, bool) or not isinstance(ack_request, int) or not 0 <= ack_request <= 1:
            raise ValueError("RIC control acknowledgement request must be either 0 or 1")

        control_header = bytes(control_header)
        control_msg = bytes(control_msg)

        requestor_id_bytes = requestor_id.to_bytes(2, byteorder="big")
        instance_id_bytes = instance_id.to_bytes(2, byteorder="big")
        ran_func_id_bytes = ran_func_id.to_bytes(2, byteorder="big")
        control_header_len = len(control_header)
        control_msg_len = len(control_msg)
        control_header_length = self._encode_aper_length(control_header_len)
        control_msg_length = self._encode_aper_length(control_msg_len)

        body = [
            0x00, 0x00, 0x05,
            0x00, 0x1d, 0x00, 0x05, 0x00, *requestor_id_bytes, *instance_id_bytes,
            0x00, 0x05, 0x00, 0x02, *ran_func_id_bytes,
            0x00, 0x16, 0x00,
            *self._encode_aper_length(len(control_header_length) + control_header_len),
            *control_header_length,
            *control_header,
            0x00, 0x17, 0x00,
            *self._encode_aper_length(len(control_msg_length) + control_msg_len),
            *control_msg_length,
            *control_msg,
            0x00, 0x15, 0x00, 0x01, ack_request << 6,
        ]

        msg = [
            0x00, 0x04, 0x00,
            *self._encode_aper_length(len(body)),
            *body,
        ]

        return bytes(msg)

    @staticmethod
    def _ran_parameter_element(parameter_id, value_type, value):
        return {
            "ranParameter-ID": parameter_id,
            "ranParameter-valueType": (
                "ranP-Choice-ElementFalse",
                {"ranParameter-value": (value_type, value)},
            ),
        }

    @staticmethod
    def _ran_parameter_structure(parameter_id, children):
        return {
            "ranParameter-ID": parameter_id,
            "ranParameter-valueType": (
                "ranP-Choice-Structure",
                {"ranParameter-Structure": {"sequence-of-ranParameters": children}},
            ),
        }

    @staticmethod
    def _ran_parameter_list(parameter_id, items):
        return {
            "ranParameter-ID": parameter_id,
            "ranParameter-valueType": (
                "ranP-Choice-List",
                {"ranParameter-List": {"list-of-ranParameter": items}},
            ),
        }

    @staticmethod
    def _validate_slice_prb_quota(quota):
        if not isinstance(quota, dict):
            raise ValueError("each slice quota must be a dictionary")

        required_fields = (
            "plmn",
            "sst",
            "min_prb_ratio",
            "max_prb_ratio",
            "dedicated_prb_ratio",
        )
        missing_fields = [field for field in required_fields if field not in quota]

        if missing_fields:
            raise ValueError(f"slice quota is missing required fields: {', '.join(missing_fields)}")

        plmn = quota["plmn"]
        sst = quota["sst"]
        sd = quota.get("sd")

        if not isinstance(plmn, str) or len(plmn) not in (5, 6) or not plmn.isdigit():
            raise ValueError("PLMN must contain five or six decimal digits")

        if isinstance(sst, bool) or not isinstance(sst, int) or not 1 <= sst <= 255:
            raise ValueError("SST must be an integer in the range 1..255")

        if sd is not None and (isinstance(sd, bool) or not isinstance(sd, int) or not 0 <= sd <= 0xFFFFFF):
            raise ValueError("SD must be absent or an integer in the range 0..16777215")

        ratios = {}

        for field in ("min_prb_ratio", "max_prb_ratio", "dedicated_prb_ratio"):
            value = quota[field]

            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 100:
                raise ValueError(f"{field} must be an integer in the range 0..100")

            ratios[field] = value

        if not ratios["dedicated_prb_ratio"] <= ratios["min_prb_ratio"] <= ratios["max_prb_ratio"]:
            raise ValueError("PRB ratios must satisfy dedicated <= minimum <= maximum")

        return {
            "plmn": plmn,
            "sst": sst,
            "sd": sd,
            **ratios,
        }

    def _build_slice_prb_ratio_group(self, quota):
        plmn = plmn_to_bytes(plmn_string_to_bcd(quota["plmn"]))
        sst = quota["sst"].to_bytes(1, byteorder="big")

        snssai_children = [
            self._ran_parameter_element(9, "valueOctS", sst),
        ]

        if quota["sd"] is not None:
            sd = quota["sd"].to_bytes(3, byteorder="big")
            snssai_children.append(self._ran_parameter_element(10, "valueOctS", sd))

        snssai = self._ran_parameter_structure(8, snssai_children)
        member = self._ran_parameter_structure(
            6,
            [
                self._ran_parameter_element(7, "valueOctS", plmn),
                snssai,
            ],
        )
        member_list = self._ran_parameter_list(
            5,
            [{"sequence-of-ranParameters": [member]}],
        )
        policy = self._ran_parameter_structure(3, [member_list])
        ratio_group = self._ran_parameter_structure(
            2,
            [
                policy,
                self._ran_parameter_element(11, "valueInt", quota["min_prb_ratio"]),
                self._ran_parameter_element(12, "valueInt", quota["max_prb_ratio"]),
                self._ran_parameter_element(13, "valueInt", quota["dedicated_prb_ratio"]),
            ],
        )

        return {"sequence-of-ranParameters": [ratio_group]}

    def build_control_request_style_2_action_6_by_slices(self, anchor_ue_id, slice_quotas, ack_request=1, requestor_id=None, instance_id=0):
        """Build one Style 2, Action 6 request containing every slice quota."""
        if isinstance(anchor_ue_id, bool) or not isinstance(anchor_ue_id, int) or not 0 <= anchor_ue_id <= 0xFFFFFFFF:
            raise ValueError("anchor UE ID must be an integer in the range 0..4294967295")

        if not isinstance(slice_quotas, (list, tuple)) or not slice_quotas:
            raise ValueError("at least one slice quota is required")

        validated_quotas = []
        seen_ssts = set()

        for raw_quota in slice_quotas:
            quota = self._validate_slice_prb_quota(raw_quota)

            if quota["sst"] in seen_ssts:
                raise ValueError(f"more than one quota was provided for SST {quota['sst']}")

            seen_ssts.add(quota["sst"])
            validated_quotas.append(quota)

        if sum(quota["min_prb_ratio"] for quota in validated_quotas) > 100:
            raise ValueError("the sum of minimum PRB ratios cannot exceed 100")

        if sum(quota["dedicated_prb_ratio"] for quota in validated_quotas) > 100:
            raise ValueError("the sum of dedicated PRB ratios cannot exceed 100")

        validated_quotas.sort(key=lambda quota: quota["sst"])
        ratio_groups = [self._build_slice_prb_ratio_group(quota) for quota in validated_quotas]
        ue_id = ("gNB-DU-UEID", {"gNB-CU-UE-F1AP-ID": anchor_ue_id})
        control_header = self.e2sm_rc_compiler.pack_ric_control_header_f1(style_type=2, control_action_id=6, ue_id_tuple=ue_id)
        control_message_definition = {
            "ric-controlMessage-formats": (
                "controlMessage-Format1",
                {"ranP-List": [self._ran_parameter_list(1, ratio_groups)]},
            ),
        }
        control_message = self.e2sm_rc_compiler.pack_ric_control_msg(control_message_definition)

        return self._build_ric_control_request(control_header, control_message, ack_request, requestor_id=requestor_id, instance_id=instance_id)

    def send_control_request_style_2_action_6_by_slices(self, e2_node_id, anchor_ue_id, slice_quotas, ack_request=1, requestor_id=None, instance_id=0):
        """Build and send one multi-slice PRB quota control request."""
        payload = self.build_control_request_style_2_action_6_by_slices(anchor_ue_id, slice_quotas, ack_request, requestor_id=requestor_id, instance_id=instance_id)
        self.parent.rmr_send(e2_node_id, payload, 12040, retries=1)
        return payload

    control_slice_level_prb_quotas = send_control_request_style_2_action_6_by_slices

    def send_control_request_style_3_action_1(self, e2_node_id, amf_ue_ngap_id, gnb_cu_ue_f1ap_id, plmn_string, target_nr_cell_id):
        # NR CGI encoding = (PLMN Identity + NR Cell Identity)
        packed_target_cgi = nr_cgi_packer.pack_nrcgi(plmn_string, target_nr_cell_id)

        PLMN = plmn_string_to_bcd(plmn_string)
        PLMN = plmn_to_bytes(PLMN)

        ue_id = ('gNB-UEID', {
            'amf-UE-NGAP-ID': amf_ue_ngap_id,
            'guami': {
                'pLMNIdentity': PLMN,
                'aMFRegionID': (b'\x00', 8),  # dummy value
                'aMFSetID':(b'\x00\x00', 10),  # dummy value
                'aMFPointer': (b'\x00', 6)  # dummy value
            },
            'gNB-CU-UE-F1AP-ID-List': [{'gNB-CU-UE-F1AP-ID': gnb_cu_ue_f1ap_id}]
        })

        control_header = self.e2sm_rc_compiler.pack_ric_control_header_f1(style_type=3, control_action_id=1, ue_id_tuple=ue_id)
        handover_msg_dict = {
            "ric-controlMessage-formats": (
                "controlMessage-Format1",
                {
                    "ranP-List": [
                        {
                            "ranParameter-ID": 1,
                            "ranParameter-valueType": (
                                "ranP-Choice-Structure",
                                {
                                    "ranParameter-Structure": {
                                        "sequence-of-ranParameters": [
                                            {
                                                "ranParameter-ID": 2,
                                                "ranParameter-valueType": (
                                                    "ranP-Choice-Structure",
                                                    {
                                                        "ranParameter-Structure": {
                                                            "sequence-of-ranParameters": [
                                                                {
                                                                    "ranParameter-ID": 3,
                                                                    "ranParameter-valueType": (
                                                                        "ranP-Choice-Structure",
                                                                        {
                                                                            "ranParameter-Structure": {
                                                                                "sequence-of-ranParameters": [
                                                                                    {
                                                                                        "ranParameter-ID": 4,
                                                                                        "ranParameter-valueType": (
                                                                                            "ranP-Choice-ElementFalse",
                                                                                            {
                                                                                                "ranParameter-value": 
                                                                                                    ("valueOctS", packed_target_cgi)
                                                                                            }
                                                                                        )
                                                                                    }
                                                                                ]
                                                                            }
                                                                        }
                                                                    )
                                                                }
                                                            ]
                                                        }
                                                    }
                                                )
                                            }
                                        ]
                                    }
                                }
                            )
                        }
                    ]
                }
            )
        }
        control_msg = self.e2sm_rc_compiler.pack_ric_control_msg(handover_msg_dict)
        payload = self._build_ric_control_request(control_header, control_msg, 1)
        self.parent.rmr_send(e2_node_id, payload, 12040, retries=1)

    def send_control_request_style_2_action_6(self, e2_node_id, ue_id, min_prb_ratio, max_prb_ratio, dedicated_prb_ratio, ack_request=1):
        plmn_string = "00101"
        sst = 1
        sd = 1

        # PLMN encoding
        PLMN = plmn_string_to_bcd(plmn_string)
        PLMN = plmn_to_bytes(PLMN)
        # S-NSSAI encoding
        sst = sst.to_bytes(1, byteorder='big')
        sd = sd.to_bytes(3, byteorder='big')

        # PRB ratio limits, i.e., [0-100]
        min_prb_ratio = max(0, min(min_prb_ratio, 100))
        max_prb_ratio = max(0, min(max_prb_ratio, 100))
        dedicated_prb_ratio = max(0, min(dedicated_prb_ratio, 100))

        if (max_prb_ratio < min_prb_ratio):
            print("ERROR: E2SM-RC Control Request - Slice Level PRB Quota: max_prb_ratio ({}) cannot be smaller than min_prb_ratio ({})".format(max_prb_ratio, min_prb_ratio))
            return

        ue_id = ('gNB-DU-UEID', {'gNB-CU-UE-F1AP-ID': ue_id})
        control_header = self.e2sm_rc_compiler.pack_ric_control_header_f1(style_type=2, control_action_id=6, ue_id_tuple=ue_id)

        control_msg_dict = {'ric-controlMessage-formats': ('controlMessage-Format1',
                                {'ranP-List': [
                                    # RRM Policy Ratio List, LIST
                                    {'ranParameter-ID': 1, 'ranParameter-valueType': ('ranP-Choice-List', {'ranParameter-List': {'list-of-ranParameter': [{'sequence-of-ranParameters': [
                                        #>RRM Policy Ratio Group, STRUCTURE
                                        {'ranParameter-ID': 2, 'ranParameter-valueType': ('ranP-Choice-Structure', {'ranParameter-Structure': {'sequence-of-ranParameters': [
                                            #>>RRM Policy, STRUCTURE
                                            {'ranParameter-ID': 3, 'ranParameter-valueType': ('ranP-Choice-Structure', {'ranParameter-Structure': {'sequence-of-ranParameters': [
                                                #Note that ID = 4 is missing in the spec.
                                                #>>RRM Policy Member List, LIST
                                                {'ranParameter-ID': 5, 'ranParameter-valueType': ('ranP-Choice-List', {'ranParameter-List': {'list-of-ranParameter': [{'sequence-of-ranParameters': [
                                                    #>>>>RRM Policy Member, STRUCTURE
                                                    {'ranParameter-ID': 6, 'ranParameter-valueType': ('ranP-Choice-Structure', {'ranParameter-Structure': {'sequence-of-ranParameters': [
                                                        #>>>>>PLMN Identity, ELEMENT
                                                        {'ranParameter-ID': 7, 'ranParameter-valueType': ('ranP-Choice-ElementFalse', {'ranParameter-value': ('valueOctS', PLMN)})},
                                                        #>>>>>S-NSSAI, STRUCTURE
                                                        {'ranParameter-ID': 8, 'ranParameter-valueType': ('ranP-Choice-Structure', {'ranParameter-Structure': {'sequence-of-ranParameters': [
                                                                #>>>>>>SST, ELEMENT
                                                                {'ranParameter-ID': 9, 'ranParameter-valueType': ('ranP-Choice-ElementFalse', {'ranParameter-value': ('valueOctS', sst)})},
                                                                #>>>>>>SD, ELEMENT
                                                                {'ranParameter-ID': 10, 'ranParameter-valueType': ('ranP-Choice-ElementFalse', {'ranParameter-value': ('valueOctS', sd)})}]
                                                            }})}]}})}]}]}})}]}})},
                                            #>>Min PRB Policy Ratio, ELEMENT
                                            {'ranParameter-ID': 11, 'ranParameter-valueType': ('ranP-Choice-ElementFalse', {'ranParameter-value': ('valueInt', min_prb_ratio)})},
                                            #>>Max PRB Policy Ratio, ELEMENT
                                            {'ranParameter-ID': 12, 'ranParameter-valueType': ('ranP-Choice-ElementFalse', {'ranParameter-value': ('valueInt', max_prb_ratio)})},
                                            #>>Dedicated PRB Policy Ratio, ELEMENT
                                            {'ranParameter-ID': 13, 'ranParameter-valueType': ('ranP-Choice-ElementFalse', {'ranParameter-value': ('valueInt', dedicated_prb_ratio)})}
                                        ]}})}
                                    ]}]}})}
                                ]}
                            )}

        control_msg = self.e2sm_rc_compiler.pack_ric_control_msg(control_msg_dict)
        payload = self._build_ric_control_request(control_header, control_msg, ack_request)
        self.parent.rmr_send(e2_node_id, payload, 12040, retries=1)

    #Controle de quotas dos PRBs por slice
    def send_control_request_style_2_action_6_by_slice(self,e2_node_id,ue_id,min_prb_ratio,max_prb_ratio,dedicated_prb_ratio,plmn_string="99970",sst=1,sd=1,ack_request=1):
        # PLMN encoding
        PLMN = plmn_string_to_bcd(plmn_string)
        PLMN = plmn_to_bytes(PLMN)
        # S-NSSAI encoding
        sst = sst.to_bytes(1, byteorder='big')
        sd = sd.to_bytes(3, byteorder='big')

        # PRB ratio limits, i.e., [0-100]
        min_prb_ratio = max(0, min(min_prb_ratio, 100))
        max_prb_ratio = max(0, min(max_prb_ratio, 100))
        dedicated_prb_ratio = max(0, min(dedicated_prb_ratio, 100))

        if (max_prb_ratio < min_prb_ratio):
            print("ERROR: E2SM-RC Control Request - Slice Level PRB Quota: max_prb_ratio ({}) cannot be smaller than min_prb_ratio ({})".format(max_prb_ratio, min_prb_ratio))
            return

        ue_id = ('gNB-DU-UEID', {'gNB-CU-UE-F1AP-ID': ue_id})
        control_header = self.e2sm_rc_compiler.pack_ric_control_header_f1(style_type=2, control_action_id=6, ue_id_tuple=ue_id)

        control_msg_dict = {'ric-controlMessage-formats': ('controlMessage-Format1',
                                {'ranP-List': [
                                    # RRM Policy Ratio List, LIST
                                    {'ranParameter-ID': 1, 'ranParameter-valueType': ('ranP-Choice-List', {'ranParameter-List': {'list-of-ranParameter': [{'sequence-of-ranParameters': [
                                        #>RRM Policy Ratio Group, STRUCTURE
                                        {'ranParameter-ID': 2, 'ranParameter-valueType': ('ranP-Choice-Structure', {'ranParameter-Structure': {'sequence-of-ranParameters': [
                                            #>>RRM Policy, STRUCTURE
                                            {'ranParameter-ID': 3, 'ranParameter-valueType': ('ranP-Choice-Structure', {'ranParameter-Structure': {'sequence-of-ranParameters': [
                                                #Note that ID = 4 is missing in the spec.
                                                #>>RRM Policy Member List, LIST
                                                {'ranParameter-ID': 5, 'ranParameter-valueType': ('ranP-Choice-List', {'ranParameter-List': {'list-of-ranParameter': [{'sequence-of-ranParameters': [
                                                    #>>>>RRM Policy Member, STRUCTURE
                                                    {'ranParameter-ID': 6, 'ranParameter-valueType': ('ranP-Choice-Structure', {'ranParameter-Structure': {'sequence-of-ranParameters': [
                                                        #>>>>>PLMN Identity, ELEMENT
                                                        {'ranParameter-ID': 7, 'ranParameter-valueType': ('ranP-Choice-ElementFalse', {'ranParameter-value': ('valueOctS', PLMN)})},
                                                        #>>>>>S-NSSAI, STRUCTURE
                                                        {'ranParameter-ID': 8, 'ranParameter-valueType': ('ranP-Choice-Structure', {'ranParameter-Structure': {'sequence-of-ranParameters': [
                                                                #>>>>>>SST, ELEMENT
                                                                {'ranParameter-ID': 9, 'ranParameter-valueType': ('ranP-Choice-ElementFalse', {'ranParameter-value': ('valueOctS', sst)})},
                                                                #>>>>>>SD, ELEMENT
                                                                {'ranParameter-ID': 10, 'ranParameter-valueType': ('ranP-Choice-ElementFalse', {'ranParameter-value': ('valueOctS', sd)})}]
                                                            }})}]}})}]}]}})}]}})},
                                            #>>Min PRB Policy Ratio, ELEMENT
                                            {'ranParameter-ID': 11, 'ranParameter-valueType': ('ranP-Choice-ElementFalse', {'ranParameter-value': ('valueInt', min_prb_ratio)})},
                                            #>>Max PRB Policy Ratio, ELEMENT
                                            {'ranParameter-ID': 12, 'ranParameter-valueType': ('ranP-Choice-ElementFalse', {'ranParameter-value': ('valueInt', max_prb_ratio)})},
                                            #>>Dedicated PRB Policy Ratio, ELEMENT
                                            {'ranParameter-ID': 13, 'ranParameter-valueType': ('ranP-Choice-ElementFalse', {'ranParameter-value': ('valueInt', dedicated_prb_ratio)})}
                                        ]}})}
                                    ]}]}})}
                                ]}
                            )}

        control_msg = self.e2sm_rc_compiler.pack_ric_control_msg(control_msg_dict)
        payload = self._build_ric_control_request(control_header, control_msg, ack_request)
        self.parent.rmr_send(e2_node_id, payload, 12040, retries=1)



    # Alias with a nice name
    control_slice_level_prb_quota = send_control_request_style_2_action_6
    control_handover = send_control_request_style_3_action_1
    # Controle de PRBs por slice
    control_slice_level_prb_quota_by_slice = send_control_request_style_2_action_6_by_slice
