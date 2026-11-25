"""类型化响应模型 —— **自动生成，请勿手改**。

由 `tools/generate_models.py` 从 `spec/response_shapes.json` 生成（51 个端点）。
那份形状是对真实租户**只读扫描**的实测产物，只记录键名与类型，不含任何值。

三条约定（都是刻意的）：

1. **所有字段都可选**。样本最多 5 行，不够断言必填；写成必填会在真实数据缺字段时
   抛校验错误，比没有类型更糟。
2. **只观测到 `null`、或出现过多种类型的字段标为 `Any`**。样本说明不了类型时不编。
3. 模型不校验业务字段：基类 `JdpModel` 是 `extra="allow"`，多出来的键原样保留。

用法::

    from jidanpu.models.generated import MODEL_FOR_PATH

    row = UserGetUserListRow.from_api(raw_dict)
    print(row.moblie)          # IDE 有补全，但取值仍可能为 None

`MODEL_FOR_PATH` 按端点路径给出对应模型；路径不在其中说明该端点没实测到字段。
"""

from __future__ import annotations

from typing import Any

from jidanpu.models.base import JdpModel

__all__ = [
    "CommonPermissionGetAliOssPolicyData",
    "CustomerGetIntervalLevelRow",
    "CustomerGetMemberAddOrderSimpInfoData",
    "EmpGetBalanceAndFreeAmountData",
    "EmpGetBindAlipaySettAuthListRow",
    "EmpGetEmpListRow",
    "EmpGetJdpBalanceData",
    "EmpGetKeFuOperationDataData",
    "EmpGetLingyuefangzhouSystemSettleBalanceData",
    "EmpGetMyEmpRow",
    "EmpGetOnLineBillSummaryListRow",
    "EmpGetOperationDataData",
    "EmpGetSalesmanStatisticsRow",
    "EmpGetSystemBuessionOperationDataData",
    "GameGetAllOrderTypeListRow",
    "GameGetGameAttrInfoTitleData",
    "GameGetGameServerMangeListRow",
    "GameGetProjectListRow",
    "GamegetGameDataRow",
    "OrderGetInternalOrderGrabbingListRow",
    "OrderGetMyOrderListRow",
    "OrderGetOrderContentByIdData",
    "OrderGetOrderListRow",
    "OrderGetOrderRecycleListRow",
    "OrderGetSoureOrderChangePriceLogListRow",
    "SmsGetSmsSettingMangeListRow",
    "SystemGetAllRoleJobListRow",
    "SystemGetAllRoleListRow",
    "SystemGetAutoDiscernSetListRow",
    "SystemGetMyBindHomeSystemListRow",
    "SystemGetOrderSoureListRow",
    "SystemGetOrderSoureMangeListRow",
    "SystemGetPlatformPushOrderConfigListRow",
    "SystemGetPushJdp_TemplateListRow",
    "SystemGetPushOrderGroupMsgSetListRow",
    "SystemGetSysmteLogListRow",
    "SystemGetSystemNoticeListRow",
    "SystemGetUserGroupListRow",
    "SystemGetUserListByOnlineConsultationSetData",
    "SystemGetVerifiAddOrderKeyStrByGameListRow",
    "SystemGetWechatAppShopFulDecListData",
    "SystemVeryJdpAccData",
    "UserGetAllUserByGameNameRow",
    "UserGetAllUserTableByGameNameRow",
    "UserGetCheckIdenticalAliPayAccRow",
    "UserGetDyplsOSSInfoForUploadFileData",
    "UserGetMyMessageListRow",
    "UserGetUserCheckInListRow",
    "UserGetUserCheckInStaticListRow",
    "UserGetUserListRow",
    "UserGetZpRootUsersRow",
    "MODEL_FOR_PATH",
]




class CommonPermissionGetAliOssPolicyData(JdpModel):
    """/CommonPermissionAjax/GetAliOssPolicy 的整体载荷（实测形状）。

    来源：空参；样本：整个响应体。
    """

    accessid: str | None = None
    expire: int | None = None
    host: str | None = None
    policy: str | None = None
    signature: str | None = None


class CustomerGetIntervalLevelRow(JdpModel):
    """/CustomerAjax/GetIntervalLevel 的列表项（实测形状）。

    来源：空参；样本 7 条。

    2 个字段只观测到 null 或多种类型，标为 Any：
        LevelDiscount、LevelNote
    """

    BusinessTypeId: int | None = None
    EndValue: float | None = None
    ExtensionStr: str | None = None
    IntervalLevelID: int | None = None
    IntervalName: str | None = None
    LevelDiscount: Any = None
    LevelNote: Any = None
    StartValue: float | None = None


class CustomerGetMemberAddOrderSimpInfoData(JdpModel):
    """/CustomerAjax/GetMemberAddOrderSimpInfo 的整体载荷（实测形状）。

    来源：空参；样本：整个响应体。

    5 个字段只观测到 null 或多种类型，标为 Any：
        firstOrderTime、lastOrderTime、membersLevelColorStr、membersLevelName、remark
    """

    balance: int | None = None
    blackCount: int | None = None
    firstOrderTime: Any = None
    hid: int | None = None
    lastOrderTime: Any = None
    membersLevelColorStr: Any = None
    membersLevelName: Any = None
    remark: Any = None
    totalConsuAmount: int | None = None


class EmpGetBalanceAndFreeAmountData(JdpModel):
    """/EmpAjax/GetBalanceAndFreeAmount 的整体载荷（实测形状）。

    来源：浏览器 HAR；样本：整个响应体。
    """

    acc: str | None = None
    b: float | None = None
    f: float | None = None


class EmpGetBindAlipaySettAuthListRow(JdpModel):
    """/EmpAjax/GetBindAlipaySettAuthList 的列表项（实测形状）。

    来源：空参；样本 1 条。

    10 个字段只观测到 null 或多种类型，标为 Any：
        company、orderId、str11、str14、str15、str16、str17、str18、str19、str20
    """

    company: Any = None
    d1: float | None = None
    d2: float | None = None
    d3: float | None = None
    d4: float | None = None
    id: int | None = None
    id10: int | None = None
    id2: int | None = None
    id3: int | None = None
    id4: int | None = None
    id5: int | None = None
    id6: int | None = None
    id7: int | None = None
    id8: int | None = None
    id9: int | None = None
    orderId: Any = None
    str1: str | None = None
    str10: str | None = None
    str11: Any = None
    str12: str | None = None
    str13: str | None = None
    str14: Any = None
    str15: Any = None
    str16: Any = None
    str17: Any = None
    str18: Any = None
    str19: Any = None
    str2: str | None = None
    str20: Any = None
    str3: str | None = None
    str4: str | None = None
    str5: str | None = None
    str6: str | None = None
    str7: str | None = None
    str8: str | None = None
    str9: str | None = None


class EmpGetEmpListRow(JdpModel):
    """/EmpAjax/GetEmpList 的列表项（实测形状）。

    来源：JS参数(核心)；样本 20 条。

    7 个字段只观测到 null 或多种类型，标为 Any：
        Js_Time、RefundTime、bankCardNo、bankName、empRemark、expectEndTimeDate、recipient
    """

    Add_Time: str | None = None
    Js_Time: Any = None
    RefundTime: Any = None
    SDataTime: str | None = None
    alipayAcc: str | None = None
    aplipayAccName: str | None = None
    applicationSett: str | None = None
    bankCardNo: Any = None
    bankName: Any = None
    bonusAmount: float | None = None
    completeTime: str | None = None
    createOrderTime: str | None = None
    createOrderTimeDate: str | None = None
    createTime: str | None = None
    deductFee: float | None = None
    dlProject: str | None = None
    empId: int | None = None
    empPayId: int | None = None
    empRemark: Any = None
    empType: str | None = None
    exceedHours: int | None = None
    expectEndTime: str | None = None
    expectEndTimeDate: Any = None
    furlStatus: str | None = None
    gameJob: str | None = None
    gameName: str | None = None
    gameRole: str | None = None
    imgCount: int | None = None
    issuePrice: int | None = None
    jionOrder: str | None = None
    kefu: str | None = None
    logUserName: str | None = None
    netReceipts: int | None = None
    orderId: str | None = None
    orderIsDel: str | None = None
    orderSoure: str | None = None
    orderSta: str | None = None
    price: float | None = None
    recipient: Any = None
    refundFee: float | None = None
    refundModel: str | None = None
    refundTimeStr: str | None = None
    refundUserName: str | None = None
    settingSta: str | None = None
    settingTime: str | None = None
    taobaoPrice: float | None = None
    taobaoSta: str | None = None
    taskId: int | None = None
    tempAmount: float | None = None
    timeLimitHours: float | None = None
    userName: str | None = None
    wangwang: str | None = None


class EmpGetJdpBalanceData(JdpModel):
    """/EmpAjax/GetJdpBalance 的整体载荷（实测形状）。

    来源：浏览器 HAR；样本：整个响应体。
    """

    code: float | None = None


class EmpGetKeFuOperationDataData(JdpModel):
    """/EmpAjax/GetKeFuOperationData 的整体载荷（实测形状）。

    来源：空参；样本：整个响应体。
    """

    code: int | None = None
    msg: dict[str, Any] | None = None


class EmpGetLingyuefangzhouSystemSettleBalanceData(JdpModel):
    """/EmpAjax/GetLingyuefangzhouSystemSettleBalance 的整体载荷（实测形状）。

    来源：空参；样本：整个响应体。
    """

    code: float | None = None


class EmpGetMyEmpRow(JdpModel):
    """/EmpAjax/GetMyEmp 的列表项（实测形状）。

    来源：浏览器 HAR；样本 20 条。

    23 个字段只观测到 null 或多种类型，标为 Any：
        Add_Time、Js_Time、RefundTime、SDataTime、alipayAcc、aplipayAccName、bankCardNo、bankName
        createOrderTime、createOrderTimeDate、empRemark、expectEndTime、expectEndTimeDate、furlStatus
        gameJob、kefu、orderSoure、recipient、refundModel、refundTimeStr、refundUserName、taobaoSta
        wangwang
    """

    Add_Time: Any = None
    Js_Time: Any = None
    RefundTime: Any = None
    SDataTime: Any = None
    alipayAcc: Any = None
    aplipayAccName: Any = None
    applicationSett: str | None = None
    bankCardNo: Any = None
    bankName: Any = None
    bonusAmount: float | None = None
    completeTime: str | None = None
    createOrderTime: Any = None
    createOrderTimeDate: Any = None
    createTime: str | None = None
    deductFee: float | None = None
    dlProject: str | None = None
    empId: int | None = None
    empPayId: int | None = None
    empRemark: Any = None
    empType: str | None = None
    exceedHours: int | None = None
    expectEndTime: Any = None
    expectEndTimeDate: Any = None
    furlStatus: Any = None
    gameJob: Any = None
    gameName: str | None = None
    gameRole: str | None = None
    imgCount: int | None = None
    issuePrice: int | None = None
    jionOrder: str | None = None
    kefu: Any = None
    logUserName: str | None = None
    netReceipts: int | None = None
    orderId: str | None = None
    orderIsDel: str | None = None
    orderSoure: Any = None
    orderSta: str | None = None
    price: int | None = None
    recipient: Any = None
    refundFee: int | None = None
    refundModel: Any = None
    refundTimeStr: Any = None
    refundUserName: Any = None
    settingSta: str | None = None
    settingTime: str | None = None
    taobaoPrice: int | None = None
    taobaoSta: Any = None
    taskId: int | None = None
    tempAmount: float | None = None
    timeLimitHours: int | None = None
    userName: str | None = None
    wangwang: Any = None


class EmpGetOnLineBillSummaryListRow(JdpModel):
    """/EmpAjax/GetOnLineBillSummaryList 的列表项（实测形状）。

    来源：浏览器 HAR；样本 4 条。

    2 个字段只观测到 null 或多种类型，标为 Any：
        empIdx、recAliPayAccName
    """

    amount: int | None = None
    createTime: str | None = None
    empIdx: Any = None
    payAmount: float | None = None
    payTranOrderNo: str | None = None
    payUserName: str | None = None
    realAmount: float | None = None
    recAliPayAccName: Any = None
    recAlipayAcc: str | None = None
    serviceAmount: float | None = None
    sid: int | None = None
    uid: int | None = None
    userName: str | None = None


class EmpGetOperationDataData(JdpModel):
    """/EmpAjax/GetOperationData 的整体载荷（实测形状）。

    来源：空参；样本：整个响应体。
    """

    code: int | None = None
    msg: dict[str, Any] | None = None


class EmpGetSalesmanStatisticsRow(JdpModel):
    """/EmpAjax/GetSalesmanStatistics 的列表项（实测形状）。

    来源：空参；样本 59 条。
    """

    AvgOrderPrice: int | None = None
    DLZ: int | None = None
    DLZAmount: float | None = None
    DSH: int | None = None
    DSHAmount: float | None = None
    DYS: int | None = None
    DYSAmount: float | None = None
    Kefu: str | None = None
    TotalCount: int | None = None
    TotalPrice: float | None = None
    WFP: int | None = None
    WFPAmount: float | None = None
    WTD: int | None = None
    WTDAmount: float | None = None
    YTD: int | None = None
    YTDAmount: float | None = None
    YWC: int | None = None
    YWCAmount: float | None = None
    YZT: int | None = None
    YZTAmount: float | None = None
    customerCount: int | None = None
    customerPrice: float | None = None
    effectivePerformance: float | None = None
    recommendCount: int | None = None
    userId: int | None = None


class EmpGetSystemBuessionOperationDataData(JdpModel):
    """/EmpAjax/GetSystemBuessionOperationData 的整体载荷（实测形状）。

    来源：空参；样本：整个响应体。
    """

    totalEmpNotSettAmount: float | None = None
    totalEmpNotSettCount: int | None = None
    totalEmpSettAmount: float | None = None
    totalEmpSettCount: int | None = None
    totalOrderCount: int | None = None
    totalPrice: float | None = None
    totalProfit: float | None = None
    totalReFundAmount: float | None = None
    totalReturnOrderCount: int | None = None
    totalReturnOrderPrice: int | None = None
    totalThrowAmount: float | None = None
    totalThrowCount: int | None = None


class GameGetAllOrderTypeListRow(JdpModel):
    """/GameAjax/GetAllOrderTypeList 的列表项（实测形状）。

    来源：浏览器 HAR；样本 1 条。

    1 个字段只观测到 null 或多种类型，标为 Any：
        selected
    """

    AppendTitleByEnd: str | None = None
    AppendTitleByFront: str | None = None
    ExStr1: str | None = None
    GameID: int | None = None
    OrderBy: int | None = None
    SkillType_ID: int | None = None
    TypeName: str | None = None
    assPlatName: str | None = None
    gameName: str | None = None
    selected: Any = None


class GameGetGameAttrInfoTitleData(JdpModel):
    """/GameAjax/GetGameAttrInfoTitle 的整体载荷（实测形状）。

    来源：浏览器 HAR；样本：整个响应体。

    20 个字段只观测到 null 或多种类型，标为 Any：
        company、orderId、str10、str11、str12、str13、str14、str15、str16、str17、str18、str19、str20、str3
        str4、str5、str6、str7、str8、str9
    """

    company: Any = None
    d1: int | None = None
    d2: int | None = None
    d3: int | None = None
    d4: int | None = None
    id: int | None = None
    id10: int | None = None
    id2: int | None = None
    id3: int | None = None
    id4: int | None = None
    id5: int | None = None
    id6: int | None = None
    id7: int | None = None
    id8: int | None = None
    id9: int | None = None
    orderId: Any = None
    str1: str | None = None
    str10: Any = None
    str11: Any = None
    str12: Any = None
    str13: Any = None
    str14: Any = None
    str15: Any = None
    str16: Any = None
    str17: Any = None
    str18: Any = None
    str19: Any = None
    str2: str | None = None
    str20: Any = None
    str3: Any = None
    str4: Any = None
    str5: Any = None
    str6: Any = None
    str7: Any = None
    str8: Any = None
    str9: Any = None


class GameGetGameServerMangeListRow(JdpModel):
    """/GameAjax/GetGameServerMangeList 的列表项（实测形状）。

    来源：空参；样本 15 条。
    """

    gameId: int | None = None
    gameName: str | None = None
    isDefulat: str | None = None
    isDisbleSendSms: int | None = None
    isMoblieGame: str | None = None
    isNotAccPwd: str | None = None
    isPw: str | None = None
    orderNum: int | None = None


class GameGetProjectListRow(JdpModel):
    """/GameAjax/GetProjectList 的列表项（实测形状）。

    来源：浏览器 HAR；样本 5 条。

    1 个字段只观测到 null 或多种类型，标为 Any：
        gameName
    """

    gameName: Any = None
    id: int | None = None
    orderBy: int | None = None
    projectName: str | None = None


class GamegetGameDataRow(JdpModel):
    """/GameAjax/getGameData 的列表项（实测形状）。

    来源：浏览器 HAR；样本 1 条。

    5 个字段只观测到 null 或多种类型，标为 Any：
        click、info1、info2、optgroup、type
    """

    brokerageType: int | None = None
    children: list[Any] | None = None
    click: Any = None
    disabled: str | None = None
    info1: Any = None
    info2: Any = None
    name: str | None = None
    optgroup: Any = None
    selected: str | None = None
    tichen: int | None = None
    tips: str | None = None
    type: Any = None
    value: str | None = None


class OrderGetInternalOrderGrabbingListRow(JdpModel):
    """/OrderAjax/GetInternalOrderGrabbingList 的列表项（实测形状）。

    来源：浏览器 HAR；样本 20 条。

    43 个字段只观测到 null 或多种类型，标为 Any：
        WangWang、accPwd、addOrderUserName、assTime、assUserName、autoAssignmentManName、completeTime
        customizeTag、delTime、delUserName、dynamicRemark、gameAcc、groupId、inputOrderManName
        isAllowChangeOrderFee、isAutoAssignment、isPushJdp、isRead、isSettStatus、isSplitOrder
        isThrowOrder、jiwei、moblie、netReceipts、orderContent、orderFromOrderId、orderLabel
        orderLabelColor、orderSoure、projectTypeName、pushJdpOrderState、qq、recManName、refundModel
        refundTimeStr、refundUserName、soureFromHost、sphone、splitId、startTime、taskName、tid
        zgameAcc
    """

    WangWang: Any = None
    accPwd: Any = None
    addOrderUserName: Any = None
    assTime: Any = None
    assUserName: Any = None
    autoAssignmentManName: Any = None
    backListId: int | None = None
    bussType: int | None = None
    completeTime: Any = None
    createTime: str | None = None
    customerId: int | None = None
    customizeTag: Any = None
    delTime: Any = None
    delUserName: Any = None
    discount: int | None = None
    dynamicRemark: Any = None
    expectEndTime: str | None = None
    fsid: int | None = None
    gameAcc: Any = None
    gameArea: str | None = None
    gameGroup: str | None = None
    gameJob: str | None = None
    gameName: str | None = None
    gameServer: str | None = None
    groupId: Any = None
    groupMangeUid: int | None = None
    hours: float | None = None
    imgCount: int | None = None
    inputOrderManName: Any = None
    isAllowChangeOrderFee: Any = None
    isAutoAssignment: Any = None
    isJiaJi: str | None = None
    isPushJdp: Any = None
    isRead: Any = None
    isSettStatus: Any = None
    isSplitOrder: Any = None
    isThrowOrder: Any = None
    itemRemark: str | None = None
    jiwei: Any = None
    levelMark: str | None = None
    moblie: Any = None
    netReceipts: Any = None
    oid: int | None = None
    orderContent: Any = None
    orderFromOrderId: Any = None
    orderHoursDescStr: str | None = None
    orderId: str | None = None
    orderIdView: str | None = None
    orderLabel: Any = None
    orderLabelColor: Any = None
    orderRemark: str | None = None
    orderSoure: Any = None
    orderSta: str | None = None
    originPrice: int | None = None
    otherRemark: str | None = None
    payMethod: int | None = None
    price: int | None = None
    project: str | None = None
    projectTypeName: Any = None
    pushJdpOrderState: Any = None
    qq: Any = None
    recFee: float | None = None
    recManName: Any = None
    recOrderInsureAmount: float | None = None
    recOrderTime: str | None = None
    recSettCount: int | None = None
    refundFee: int | None = None
    refundModel: Any = None
    refundTimeStr: Any = None
    refundUserName: Any = None
    roleName: str | None = None
    serviceAmount: int | None = None
    soureFromHost: Any = None
    sphone: Any = None
    splitId: Any = None
    startTime: Any = None
    taskId: int | None = None
    taskName: Any = None
    throwCount: int | None = None
    tid: Any = None
    transfType: int | None = None
    zgameAcc: Any = None


class OrderGetMyOrderListRow(JdpModel):
    """/OrderAjax/GetMyOrderList 的列表项（实测形状）。

    来源：浏览器 HAR；样本 9 条。

    30 个字段只观测到 null 或多种类型，标为 Any：
        WangWang、addOrderUserName、assTime、assUserName、completeTime、delTime、delUserName、gameAcc
        inputOrderManName、isAllowChangeOrderFee、isPushJdp、isSettStatus、isThrowOrder、moblie
        netReceipts、orderFromOrderId、orderLabel、orderLabelColor、orderSoure、projectTypeName
        pushJdpOrderState、qq、refundModel、refundTimeStr、refundUserName、soureFromHost、splitId
        taskName、tid、zgameAcc
    """

    WangWang: Any = None
    accPwd: str | None = None
    addOrderUserName: Any = None
    assTime: Any = None
    assUserName: Any = None
    autoAssignmentManName: str | None = None
    backListId: int | None = None
    bussType: int | None = None
    completeTime: Any = None
    createTime: str | None = None
    customerId: int | None = None
    customizeTag: str | None = None
    delTime: Any = None
    delUserName: Any = None
    discount: int | None = None
    dynamicRemark: str | None = None
    expectEndTime: str | None = None
    fsid: int | None = None
    gameAcc: Any = None
    gameArea: str | None = None
    gameGroup: str | None = None
    gameJob: str | None = None
    gameName: str | None = None
    gameServer: str | None = None
    groupId: str | None = None
    groupMangeUid: int | None = None
    hours: float | None = None
    imgCount: int | None = None
    inputOrderManName: Any = None
    isAllowChangeOrderFee: Any = None
    isAutoAssignment: str | None = None
    isJiaJi: str | None = None
    isPushJdp: Any = None
    isRead: str | None = None
    isSettStatus: Any = None
    isSplitOrder: str | None = None
    isThrowOrder: Any = None
    itemRemark: str | None = None
    jiwei: str | None = None
    levelMark: str | None = None
    moblie: Any = None
    netReceipts: Any = None
    oid: int | None = None
    orderContent: str | None = None
    orderFromOrderId: Any = None
    orderHoursDescStr: str | None = None
    orderId: str | None = None
    orderIdView: str | None = None
    orderLabel: Any = None
    orderLabelColor: Any = None
    orderRemark: str | None = None
    orderSoure: Any = None
    orderSta: str | None = None
    originPrice: int | None = None
    otherRemark: str | None = None
    payMethod: int | None = None
    price: int | None = None
    project: str | None = None
    projectTypeName: Any = None
    pushJdpOrderState: Any = None
    qq: Any = None
    recFee: float | None = None
    recManName: str | None = None
    recOrderInsureAmount: int | None = None
    recOrderTime: str | None = None
    recSettCount: int | None = None
    refundFee: int | None = None
    refundModel: Any = None
    refundTimeStr: Any = None
    refundUserName: Any = None
    roleName: str | None = None
    serviceAmount: int | None = None
    soureFromHost: Any = None
    sphone: str | None = None
    splitId: Any = None
    startTime: str | None = None
    taskId: int | None = None
    taskName: Any = None
    throwCount: int | None = None
    tid: Any = None
    transfType: int | None = None
    zgameAcc: Any = None


class OrderGetOrderContentByIdData(JdpModel):
    """/OrderAjax/GetOrderContentById 的整体载荷（实测形状）。

    来源：空参；样本：整个响应体。
    """

    code: int | None = None
    msg: str | None = None


class OrderGetOrderListRow(JdpModel):
    """/OrderAjax/GetOrderList 的列表项（实测形状）。

    来源：JS参数(核心)；样本 5 条。

    20 个字段只观测到 null 或多种类型，标为 Any：
        accPwd、autoAssignmentManName、delTime、delUserName、dynamicRemark、gameAcc、groupId、jiwei
        moblie、orderFromOrderId、orderLabelColor、pushJdpOrderState、recOrderTime、refundTimeStr
        soureFromHost、sphone、splitId、taskName、tid、zgameAcc
    """

    WangWang: str | None = None
    accPwd: Any = None
    addOrderUserName: str | None = None
    assTime: str | None = None
    assUserName: str | None = None
    autoAssignmentManName: Any = None
    backListId: int | None = None
    bussType: int | None = None
    completeTime: str | None = None
    createTime: str | None = None
    customerId: int | None = None
    customizeTag: str | None = None
    delTime: Any = None
    delUserName: Any = None
    discount: float | None = None
    dynamicRemark: Any = None
    expectEndTime: str | None = None
    fsid: int | None = None
    gameAcc: Any = None
    gameArea: str | None = None
    gameGroup: str | None = None
    gameJob: str | None = None
    gameName: str | None = None
    gameServer: str | None = None
    groupId: Any = None
    groupMangeUid: int | None = None
    hours: float | None = None
    imgCount: int | None = None
    inputOrderManName: str | None = None
    isAllowChangeOrderFee: str | None = None
    isAutoAssignment: str | None = None
    isJiaJi: str | None = None
    isPushJdp: str | None = None
    isRead: str | None = None
    isSettStatus: str | None = None
    isSplitOrder: str | None = None
    isThrowOrder: str | None = None
    itemRemark: str | None = None
    jiwei: Any = None
    levelMark: str | None = None
    moblie: Any = None
    netReceipts: str | None = None
    oid: int | None = None
    orderContent: str | None = None
    orderFromOrderId: Any = None
    orderHoursDescStr: str | None = None
    orderId: str | None = None
    orderIdView: str | None = None
    orderLabel: str | None = None
    orderLabelColor: Any = None
    orderRemark: str | None = None
    orderSoure: str | None = None
    orderSta: str | None = None
    originPrice: float | None = None
    otherRemark: str | None = None
    payMethod: int | None = None
    price: float | None = None
    project: str | None = None
    projectTypeName: str | None = None
    pushJdpOrderState: Any = None
    qq: str | None = None
    recFee: float | None = None
    recManName: str | None = None
    recOrderInsureAmount: int | None = None
    recOrderTime: Any = None
    recSettCount: int | None = None
    refundFee: float | None = None
    refundModel: str | None = None
    refundTimeStr: Any = None
    refundUserName: str | None = None
    roleName: str | None = None
    serviceAmount: int | None = None
    soureFromHost: Any = None
    sphone: Any = None
    splitId: Any = None
    startTime: str | None = None
    taskId: int | None = None
    taskName: Any = None
    throwCount: int | None = None
    tid: Any = None
    transfType: int | None = None
    zgameAcc: Any = None


class OrderGetOrderRecycleListRow(JdpModel):
    """/OrderAjax/GetOrderRecycleList 的列表项（实测形状）。

    来源：JS参数(核心)；样本 20 条。

    40 个字段只观测到 null 或多种类型，标为 Any：
        accPwd、addOrderUserName、assTime、assUserName、autoAssignmentManName、completeTime
        dynamicRemark、gameAcc、groupId、inputOrderManName、isAllowChangeOrderFee、isAutoAssignment
        isPushJdp、isRead、isSettStatus、isSplitOrder、isThrowOrder、jiwei、moblie、netReceipts
        orderContent、orderFromOrderId、orderHoursDescStr、orderLabel、orderLabelColor
        projectTypeName、pushJdpOrderState、qq、recManName、recOrderTime、refundModel、refundTimeStr
        refundUserName、soureFromHost、sphone、splitId、startTime、taskName、tid、zgameAcc
    """

    WangWang: str | None = None
    accPwd: Any = None
    addOrderUserName: Any = None
    assTime: Any = None
    assUserName: Any = None
    autoAssignmentManName: Any = None
    backListId: int | None = None
    bussType: int | None = None
    completeTime: Any = None
    createTime: str | None = None
    customerId: int | None = None
    customizeTag: str | None = None
    delTime: str | None = None
    delUserName: str | None = None
    discount: int | None = None
    dynamicRemark: Any = None
    expectEndTime: str | None = None
    fsid: int | None = None
    gameAcc: Any = None
    gameArea: str | None = None
    gameGroup: str | None = None
    gameJob: str | None = None
    gameName: str | None = None
    gameServer: str | None = None
    groupId: Any = None
    groupMangeUid: int | None = None
    hours: int | None = None
    imgCount: int | None = None
    inputOrderManName: Any = None
    isAllowChangeOrderFee: Any = None
    isAutoAssignment: Any = None
    isJiaJi: str | None = None
    isPushJdp: Any = None
    isRead: Any = None
    isSettStatus: Any = None
    isSplitOrder: Any = None
    isThrowOrder: Any = None
    itemRemark: str | None = None
    jiwei: Any = None
    levelMark: str | None = None
    moblie: Any = None
    netReceipts: Any = None
    oid: int | None = None
    orderContent: Any = None
    orderFromOrderId: Any = None
    orderHoursDescStr: Any = None
    orderId: str | None = None
    orderIdView: str | None = None
    orderLabel: Any = None
    orderLabelColor: Any = None
    orderRemark: str | None = None
    orderSoure: str | None = None
    orderSta: str | None = None
    originPrice: int | None = None
    otherRemark: str | None = None
    payMethod: int | None = None
    price: int | None = None
    project: str | None = None
    projectTypeName: Any = None
    pushJdpOrderState: Any = None
    qq: Any = None
    recFee: int | None = None
    recManName: Any = None
    recOrderInsureAmount: int | None = None
    recOrderTime: Any = None
    recSettCount: int | None = None
    refundFee: int | None = None
    refundModel: Any = None
    refundTimeStr: Any = None
    refundUserName: Any = None
    roleName: str | None = None
    serviceAmount: int | None = None
    soureFromHost: Any = None
    sphone: Any = None
    splitId: Any = None
    startTime: Any = None
    taskId: int | None = None
    taskName: Any = None
    throwCount: int | None = None
    tid: Any = None
    transfType: int | None = None
    zgameAcc: Any = None


class OrderGetSoureOrderChangePriceLogListRow(JdpModel):
    """/OrderAjax/GetSoureOrderChangePriceLogList 的列表项（实测形状）。

    来源：浏览器 HAR；样本 20 条。

    15 个字段只观测到 null 或多种类型，标为 Any：
        company、str10、str11、str12、str13、str14、str15、str16、str17、str18、str19、str20、str7、str8、str9
    """

    company: Any = None
    d1: float | None = None
    d2: float | None = None
    d3: int | None = None
    d4: int | None = None
    id: int | None = None
    id10: int | None = None
    id2: int | None = None
    id3: int | None = None
    id4: int | None = None
    id5: int | None = None
    id6: int | None = None
    id7: int | None = None
    id8: int | None = None
    id9: int | None = None
    orderId: str | None = None
    str1: str | None = None
    str10: Any = None
    str11: Any = None
    str12: Any = None
    str13: Any = None
    str14: Any = None
    str15: Any = None
    str16: Any = None
    str17: Any = None
    str18: Any = None
    str19: Any = None
    str2: str | None = None
    str20: Any = None
    str3: str | None = None
    str4: str | None = None
    str5: str | None = None
    str6: str | None = None
    str7: Any = None
    str8: Any = None
    str9: Any = None


class SmsGetSmsSettingMangeListRow(JdpModel):
    """/SmsAjax/GetSmsSettingMangeList 的列表项（实测形状）。

    来源：空参；样本 15 条。
    """

    GameName: str | None = None
    SelectTbSwicth: int | None = None
    SendOpport: int | None = None
    SmsContent: str | None = None
    SmsSetting_Id: int | None = None
    SmsSwicth: int | None = None
    SmsTitle: str | None = None
    SmsType: int | None = None


class SystemGetAllRoleJobListRow(JdpModel):
    """/SystemAjax/GetAllRoleJobList 的列表项（实测形状）。

    来源：空参；样本 9 条。
    """

    remark: str | None = None
    roleId: int | None = None
    roleName: str | None = None


class SystemGetAllRoleListRow(JdpModel):
    """/SystemAjax/GetAllRoleList 的列表项（实测形状）。

    来源：浏览器 HAR；样本 16 条。

    5 个字段只观测到 null 或多种类型，标为 Any：
        click、info1、info2、optgroup、type
    """

    brokerageType: int | None = None
    children: list[Any] | None = None
    click: Any = None
    disabled: str | None = None
    info1: Any = None
    info2: Any = None
    name: str | None = None
    optgroup: Any = None
    selected: str | None = None
    tichen: int | None = None
    tips: str | None = None
    type: Any = None
    value: str | None = None


class SystemGetAutoDiscernSetListRow(JdpModel):
    """/SystemAjax/GetAutoDiscernSetList 的列表项（实测形状）。

    来源：空参；样本 18 条。
    """

    disId: int | None = None
    disKey: str | None = None
    typeName: str | None = None


class SystemGetMyBindHomeSystemListRow(JdpModel):
    """/SystemAjax/GetMyBindHomeSystemList 的列表项（实测形状）。

    来源：浏览器 HAR；样本 7 条。
    """

    BindHomeSystemLogId: int | None = None
    BindTimeStr: str | None = None
    CreateTime: str | None = None
    CreateUid: int | None = None
    HomeAcc: str | None = None
    HomeCompany: str | None = None
    HomeHost: str | None = None
    HomeUid: int | None = None
    UserName: str | None = None


class SystemGetOrderSoureListRow(JdpModel):
    """/SystemAjax/GetOrderSoureList 的列表项（实测形状）。

    来源：浏览器 HAR；样本 20 条。

    5 个字段只观测到 null 或多种类型，标为 Any：
        click、info1、info2、optgroup、type
    """

    brokerageType: int | None = None
    children: list[Any] | None = None
    click: Any = None
    disabled: str | None = None
    info1: Any = None
    info2: Any = None
    name: str | None = None
    optgroup: Any = None
    selected: str | None = None
    tichen: int | None = None
    tips: str | None = None
    type: Any = None
    value: str | None = None


class SystemGetOrderSoureMangeListRow(JdpModel):
    """/SystemAjax/GetOrderSoureMangeList 的列表项（实测形状）。

    来源：空参；样本 14 条。
    """

    doNotSendSms: str | None = None
    id: int | None = None
    orderBy: int | None = None
    remark: str | None = None
    shopType: int | None = None
    smsSign: str | None = None
    soureName: str | None = None


class SystemGetPlatformPushOrderConfigListRow(JdpModel):
    """/SystemAjax/GetPlatformPushOrderConfigList 的列表项（实测形状）。

    来源：空参；样本 14 条。

    1 个字段只观测到 null 或多种类型，标为 Any：
        pushPlatformIds
    """

    gameName: str | None = None
    id: int | None = None
    isAutoWritePhone: int | None = None
    pushPlatformEffAmount: float | None = None
    pushPlatformIds: Any = None
    pushPlatformNames: str | None = None
    pushPlatformPhone: str | None = None
    pushPlatformQQ: str | None = None
    pushPlatformRequire: str | None = None
    pushPlatformSafeAmount: float | None = None
    pushPlatfromBuilderPrivePhone: int | None = None
    pushPlatfromPriceScale: int | None = None


class SystemGetPushJdp_TemplateListRow(JdpModel):
    """/SystemAjax/GetPushJdp_TemplateList 的列表项（实测形状）。

    来源：浏览器 HAR；样本 2 条。
    """

    content: str | None = None
    isdef: str | None = None
    platformType: int | None = None
    tid: int | None = None
    title: str | None = None


class SystemGetPushOrderGroupMsgSetListRow(JdpModel):
    """/SystemAjax/GetPushOrderGroupMsgSetList 的列表项（实测形状）。

    来源：空参；样本 1 条。

    1 个字段只观测到 null 或多种类型，标为 Any：
        groupId
    """

    callAll: int | None = None
    gameName: str | None = None
    groupId: Any = None
    id: int | None = None
    kurl: int | None = None
    msgType: int | None = None
    remark: str | None = None
    replyOcc: int | None = None
    switchStatus: int | None = None
    webhookUrl: str | None = None


class SystemGetSysmteLogListRow(JdpModel):
    """/SystemAjax/GetSysmteLogList 的列表项（实测形状）。

    来源：浏览器 HAR；样本 30 条。
    """

    createTime: str | None = None
    gameName: str | None = None
    gameRoleName: str | None = None
    joinOrderId: str | None = None
    logType: int | None = None
    remark: str | None = None
    soureOrderId: str | None = None
    userName: str | None = None


class SystemGetSystemNoticeListRow(JdpModel):
    """/SystemAjax/GetSystemNoticeList 的列表项（实测形状）。

    来源：浏览器 HAR；样本 3 条。
    """

    content: str | None = None
    createTime: str | None = None
    id: int | None = None
    title: str | None = None


class SystemGetUserGroupListRow(JdpModel):
    """/SystemAjax/GetUserGroupList 的列表项（实测形状）。

    来源：空参；样本 6 条。
    """

    containJobName: str | None = None
    groupId: int | None = None
    groupName: str | None = None


class SystemGetUserListByOnlineConsultationSetData(JdpModel):
    """/SystemAjax/GetUserListByOnlineConsultationSet 的整体载荷（实测形状）。

    来源：空参；样本：整个响应体。
    """

    code: int | None = None
    data1: list[Any] | None = None
    data2: list[Any] | None = None
    data3: list[Any] | None = None
    data4: list[Any] | None = None


class SystemGetVerifiAddOrderKeyStrByGameListRow(JdpModel):
    """/SystemAjax/GetVerifiAddOrderKeyStrByGameList 的列表项（实测形状）。

    来源：空参；样本 25 条。

    1 个字段只观测到 null 或多种类型，标为 Any：
        gameName
    """

    gameName: Any = None
    id: int | None = None
    key: str | None = None
    resultStr: str | None = None
    veriType: int | None = None


class SystemGetWechatAppShopFulDecListData(JdpModel):
    """/SystemAjax/GetWechatAppShopFulDecList 的整体载荷（实测形状）。

    来源：空参；样本：整个响应体。
    """

    code: int | None = None
    fullDecData: list[Any] | None = None
    hyDisCData: list[Any] | None = None


class SystemVeryJdpAccData(JdpModel):
    """/SystemAjax/VeryJdpAcc 的整体载荷（实测形状）。

    来源：空参；样本：整个响应体。
    """

    code: int | None = None
    msg: str | None = None


class UserGetAllUserByGameNameRow(JdpModel):
    """/UserAjax/GetAllUserByGameName 的列表项（实测形状）。

    来源：浏览器 HAR；样本 1000 条。

    4 个字段只观测到 null 或多种类型，标为 Any：
        click、info1、optgroup、type
    """

    brokerageType: int | None = None
    children: list[Any] | None = None
    click: Any = None
    disabled: str | None = None
    info1: Any = None
    info2: str | None = None
    name: str | None = None
    optgroup: Any = None
    selected: str | None = None
    tichen: float | None = None
    tips: str | None = None
    type: Any = None
    value: str | None = None


class UserGetAllUserTableByGameNameRow(JdpModel):
    """/UserAjax/GetAllUserTableByGameName 的列表项（实测形状）。

    来源：空参；样本 102 条。

    2 个字段只观测到 null 或多种类型，标为 Any：
        pic、taskIsHaveMe
    """

    brokerageType: int | None = None
    games: str | None = None
    id: int | None = None
    inputJobDate: str | None = None
    isAbsolveLoginVer: str | None = None
    isValid: str | None = None
    loginAcc: str | None = None
    moblie: str | None = None
    outJobDate: str | None = None
    pic: Any = None
    qq: str | None = None
    recOrderMaxCount: int | None = None
    remark: str | None = None
    roleName: str | None = None
    taskIsHaveMe: Any = None
    tichen: float | None = None
    tips: str | None = None
    userName: str | None = None
    userType: int | None = None


class UserGetCheckIdenticalAliPayAccRow(JdpModel):
    """/UserAjax/GetCheckIdenticalAliPayAcc 的列表项（实测形状）。

    来源：浏览器 HAR；样本 184 条。

    18 个字段只观测到 null 或多种类型，标为 Any：
        company、orderId、str10、str11、str12、str13、str14、str15、str16、str17、str18、str19、str20、str5
        str6、str7、str8、str9
    """

    company: Any = None
    d1: int | None = None
    d2: int | None = None
    d3: int | None = None
    d4: int | None = None
    id: int | None = None
    id10: int | None = None
    id2: int | None = None
    id3: int | None = None
    id4: int | None = None
    id5: int | None = None
    id6: int | None = None
    id7: int | None = None
    id8: int | None = None
    id9: int | None = None
    orderId: Any = None
    str1: str | None = None
    str10: Any = None
    str11: Any = None
    str12: Any = None
    str13: Any = None
    str14: Any = None
    str15: Any = None
    str16: Any = None
    str17: Any = None
    str18: Any = None
    str19: Any = None
    str2: str | None = None
    str20: Any = None
    str3: str | None = None
    str4: str | None = None
    str5: Any = None
    str6: Any = None
    str7: Any = None
    str8: Any = None
    str9: Any = None


class UserGetDyplsOSSInfoForUploadFileData(JdpModel):
    """/UserAjax/GetDyplsOSSInfoForUploadFile 的整体载荷（实测形状）。

    来源：空参；样本：整个响应体。
    """

    AccessKeyId: str | None = None
    ExpireTime: str | None = None
    Host: str | None = None
    Policy: str | None = None
    Signature: str | None = None
    StartPath: str | None = None


class UserGetMyMessageListRow(JdpModel):
    """/UserAjax/GetMyMessageList 的列表项（实测形状）。

    来源：空参；样本 1 条。

    2 个字段只观测到 null 或多种类型，标为 Any：
        isAutoPaySound、isViewNotification
    """

    content: str | None = None
    createTime: str | None = None
    isAutoPaySound: Any = None
    isRead: str | None = None
    isViewNotification: Any = None
    msgId: int | None = None
    msgType: int | None = None
    orderId: str | None = None
    sendUserName: str | None = None
    title: str | None = None


class UserGetUserCheckInListRow(JdpModel):
    """/UserAjax/GetUserCheckInList 的列表项（实测形状）。

    来源：空参；样本 20 条。
    """

    checkType: int | None = None
    checkinNumber: int | None = None
    computer: str | None = None
    createTime: str | None = None
    ip: str | None = None
    loginAcc: str | None = None
    remark: str | None = None
    roleName: str | None = None
    userName: str | None = None


class UserGetUserCheckInStaticListRow(JdpModel):
    """/UserAjax/GetUserCheckInStaticList 的列表项（实测形状）。

    来源：空参；样本 20 条。

    4 个字段只观测到 null 或多种类型，标为 Any：
        computer、createTime、ip、remark
    """

    checkType: int | None = None
    checkinNumber: int | None = None
    computer: Any = None
    createTime: Any = None
    ip: Any = None
    loginAcc: str | None = None
    remark: Any = None
    roleName: str | None = None
    userName: str | None = None


class UserGetUserListRow(JdpModel):
    """/UserAjax/GetUserList 的列表项（实测形状）。

    来源：浏览器 HAR；样本 20 条。
    """

    balance: float | None = None
    bussType: int | None = None
    complateRate: float | None = None
    createUserName: str | None = None
    disableDate: str | None = None
    entryDate: str | None = None
    forbidViewOrderContent: int | None = None
    games: str | None = None
    insureAmount: float | None = None
    isAutoSett: str | None = None
    isExcuseLoginVer: str | None = None
    isNeedPrivatePhone: str | None = None
    isProhibitPublicRecOrder: int | None = None
    isProhibitSettStatus: str | None = None
    isRecEarlyWarningSms: str | None = None
    jdpUid: int | None = None
    jobName: str | None = None
    lastLoginTime: str | None = None
    loginAcc: str | None = None
    mark: str | None = None
    moblie: str | None = None
    orderBy: int | None = None
    pic: str | None = None
    prohibitedViewOrderPwd: str | None = None
    qq: str | None = None
    recOrderMaxCount: int | None = None
    recPriceMaxValue: float | None = None
    recPriceMinValue: float | None = None
    recommend: str | None = None
    remark: str | None = None
    rname: str | None = None
    rnameCertification: str | None = None
    roleId: int | None = None
    signStatus: int | None = None
    sta: str | None = None
    uid: int | None = None
    userName: str | None = None
    userType: int | None = None


class UserGetZpRootUsersRow(JdpModel):
    """/UserAjax/GetZpRootUsers 的列表项（实测形状）。

    来源：空参；样本 100 条。

    6 个字段只观测到 null 或多种类型，标为 Any：
        click、disabled、info1、optgroup、selected、type
    """

    brokerageType: int | None = None
    children: list[Any] | None = None
    click: Any = None
    disabled: Any = None
    info1: Any = None
    info2: str | None = None
    name: str | None = None
    optgroup: Any = None
    selected: Any = None
    tichen: int | None = None
    tips: str | None = None
    type: Any = None
    value: str | None = None


MODEL_FOR_PATH: dict[str, type[JdpModel]] = {
    '/CommonPermissionAjax/GetAliOssPolicy':
        CommonPermissionGetAliOssPolicyData,
    '/CustomerAjax/GetIntervalLevel':
        CustomerGetIntervalLevelRow,
    '/CustomerAjax/GetMemberAddOrderSimpInfo':
        CustomerGetMemberAddOrderSimpInfoData,
    '/EmpAjax/GetBalanceAndFreeAmount':
        EmpGetBalanceAndFreeAmountData,
    '/EmpAjax/GetBindAlipaySettAuthList':
        EmpGetBindAlipaySettAuthListRow,
    '/EmpAjax/GetEmpList':
        EmpGetEmpListRow,
    '/EmpAjax/GetJdpBalance':
        EmpGetJdpBalanceData,
    '/EmpAjax/GetKeFuOperationData':
        EmpGetKeFuOperationDataData,
    '/EmpAjax/GetLingyuefangzhouSystemSettleBalance':
        EmpGetLingyuefangzhouSystemSettleBalanceData,
    '/EmpAjax/GetMyEmp':
        EmpGetMyEmpRow,
    '/EmpAjax/GetOnLineBillSummaryList':
        EmpGetOnLineBillSummaryListRow,
    '/EmpAjax/GetOperationData':
        EmpGetOperationDataData,
    '/EmpAjax/GetSalesmanStatistics':
        EmpGetSalesmanStatisticsRow,
    '/EmpAjax/GetSystemBuessionOperationData':
        EmpGetSystemBuessionOperationDataData,
    '/GameAjax/GetAllOrderTypeList':
        GameGetAllOrderTypeListRow,
    '/GameAjax/GetGameAttrInfoTitle':
        GameGetGameAttrInfoTitleData,
    '/GameAjax/GetGameServerMangeList':
        GameGetGameServerMangeListRow,
    '/GameAjax/GetProjectList':
        GameGetProjectListRow,
    '/GameAjax/getGameData':
        GamegetGameDataRow,
    '/OrderAjax/GetInternalOrderGrabbingList':
        OrderGetInternalOrderGrabbingListRow,
    '/OrderAjax/GetMyOrderList':
        OrderGetMyOrderListRow,
    '/OrderAjax/GetOrderContentById':
        OrderGetOrderContentByIdData,
    '/OrderAjax/GetOrderList':
        OrderGetOrderListRow,
    '/OrderAjax/GetOrderRecycleList':
        OrderGetOrderRecycleListRow,
    '/OrderAjax/GetSoureOrderChangePriceLogList':
        OrderGetSoureOrderChangePriceLogListRow,
    '/SmsAjax/GetSmsSettingMangeList':
        SmsGetSmsSettingMangeListRow,
    '/SystemAjax/GetAllRoleJobList':
        SystemGetAllRoleJobListRow,
    '/SystemAjax/GetAllRoleList':
        SystemGetAllRoleListRow,
    '/SystemAjax/GetAutoDiscernSetList':
        SystemGetAutoDiscernSetListRow,
    '/SystemAjax/GetMyBindHomeSystemList':
        SystemGetMyBindHomeSystemListRow,
    '/SystemAjax/GetOrderSoureList':
        SystemGetOrderSoureListRow,
    '/SystemAjax/GetOrderSoureMangeList':
        SystemGetOrderSoureMangeListRow,
    '/SystemAjax/GetPlatformPushOrderConfigList':
        SystemGetPlatformPushOrderConfigListRow,
    '/SystemAjax/GetPushJdp_TemplateList':
        SystemGetPushJdp_TemplateListRow,
    '/SystemAjax/GetPushOrderGroupMsgSetList':
        SystemGetPushOrderGroupMsgSetListRow,
    '/SystemAjax/GetSysmteLogList':
        SystemGetSysmteLogListRow,
    '/SystemAjax/GetSystemNoticeList':
        SystemGetSystemNoticeListRow,
    '/SystemAjax/GetUserGroupList':
        SystemGetUserGroupListRow,
    '/SystemAjax/GetUserListByOnlineConsultationSet':
        SystemGetUserListByOnlineConsultationSetData,
    '/SystemAjax/GetVerifiAddOrderKeyStrByGameList':
        SystemGetVerifiAddOrderKeyStrByGameListRow,
    '/SystemAjax/GetWechatAppShopFulDecList':
        SystemGetWechatAppShopFulDecListData,
    '/SystemAjax/VeryJdpAcc':
        SystemVeryJdpAccData,
    '/UserAjax/GetAllUserByGameName':
        UserGetAllUserByGameNameRow,
    '/UserAjax/GetAllUserTableByGameName':
        UserGetAllUserTableByGameNameRow,
    '/UserAjax/GetCheckIdenticalAliPayAcc':
        UserGetCheckIdenticalAliPayAccRow,
    '/UserAjax/GetDyplsOSSInfoForUploadFile':
        UserGetDyplsOSSInfoForUploadFileData,
    '/UserAjax/GetMyMessageList':
        UserGetMyMessageListRow,
    '/UserAjax/GetUserCheckInList':
        UserGetUserCheckInListRow,
    '/UserAjax/GetUserCheckInStaticList':
        UserGetUserCheckInStaticListRow,
    '/UserAjax/GetUserList':
        UserGetUserListRow,
    '/UserAjax/GetZpRootUsers':
        UserGetZpRootUsersRow,
}
