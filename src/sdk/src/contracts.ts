export interface PageQuery {
  page?: number;
  page_size?: number;
}

export interface Page<T> {
  count: number;
  results: T[];
  page: number;
  page_size: number;
}

export type Decimal = string;
export type Timestamp = string;

export interface Order {
  order_no: string;
  source_order_no: string;
  status: string;
  status_text: string;
  game: string;
  project: string | null;
  total_price: Decimal | null;
  created_at: Timestamp;
  updated_at: Timestamp;
  source: string;
  game_id: number;
  server_id: number | null;
  project_id: number | null;
  taker_id: number | null;
  deleted_at: Timestamp | null;
  taker_name: string | null;
  submitted_at: Timestamp | null;
  published_at: Timestamp | null;
  amounts: {
    total_price: Decimal | null;
    hall_price: Decimal | null;
  };
  content: {
    content: string;
    demand: string;
    time_requirement: string;
  };
  order: {
    order_type: string;
    server: string | null;
    category: string | null;
    project_text: string;
    is_urgent: boolean;
    tag: string;
    completed_at: Timestamp | null;
  };
  player: {
    game_account: string;
    game_sub_account: string;
    game_role_name: string;
    current_info_1: string;
    current_info_2: string;
  };
  customer: {
    customer_phone: string;
    customer_qq: string;
    customer_ww: string;
  };
}

export interface OrderFields {
  game?: number;
  server?: number | null;
  category?: number | null;
  project?: number | null;
  source?: string;
  source_order_no?: string;
  project_text?: string;
  total_price?: Decimal;
  content?: string;
  remark?: string;
  demand?: string;
  time_requirement?: string;
  game_account?: string;
  game_sub_account?: string;
  game_role_name?: string;
  current_info_1?: string;
  current_info_2?: string;
  customer_phone?: string;
  customer_qq?: string;
  customer_ww?: string;
  order_type?: string;
  is_urgent?: boolean;
}

export interface OrderCreate extends OrderFields {
  game: number;
  project_text: string;
  total_price: Decimal;
  source_order_no: string;
  hall_price?: Decimal | null;
}

export interface OrderPatch extends OrderFields {
  tag?: string;
}

export interface OrderQuery extends PageQuery {
  after?: string;
  status?: string;
  game_id?: number;
  taker_id?: number;
  source?: string;
  source_order_no?: string;
  updated_since?: Timestamp;
  created_from?: Timestamp;
  created_to?: Timestamp;
}

export type OperatorStatus = "PENDING_REVIEW" | "COMPLETED" | "ISSUE" | "PAUSED" | "REFUNDED";
export type TakerStatus = "PENDING_REVIEW" | "ISSUE" | "PAUSED";

export interface StatusChange<State extends string = OperatorStatus> {
  status: State;
  remark?: string;
}

export interface OrderState {
  order_no: string;
  status: string;
  status_text: string;
}

export interface OrderTiming {
  status: string;
  status_text: string;
  created_at: Timestamp | null;
  claimed_at: Timestamp | null;
  submitted_at: Timestamp | null;
  completed_at: Timestamp | null;
}

export type ImageExt = "jpg" | "jpeg" | "png";

export interface ImageTicket {
  key: string;
  upload_url: string;
  expires_in: number;
}

export interface SplitOrderResult {
  order_no: string;
  split_id: string;
  created: Array<{ order_no: string; source_order_no: string }>;
}

export interface PeerSplitNewOrder {
  project_text: string;
  total_price: Decimal;
}

export type PeerSplitExistingOrder = { order_no: string } & (
  | { project_text: string; total_price?: Decimal }
  | { project_text?: string; total_price: Decimal }
);

export interface PeerSplitOrder {
  remaining_project_text: string;
  source_total_price?: Decimal;
  new_orders?: PeerSplitNewOrder[];
  existing_orders?: PeerSplitExistingOrder[];
}

export interface PeerSplitResult extends SplitOrderResult {
  split_kind: "PEER" | "LEGACY";
  source_total_price: Decimal;
  updated: string[];
  members: Array<PeerSplitNewOrder & {
    order_no: string;
    split_is_balance_anchor: boolean;
  }>;
}

export interface OrderImage {
  img_type: number;
  url: string;
  original_name: string;
  size_bytes: number;
  remark: string;
  uploaded_by_name: string;
  created_at: Timestamp;
}

export interface ManagedOrderImage extends OrderImage {
  id: number;
}

export interface OrderAudit {
  id: number;
  actor: string;
  action: string;
  created_at: Timestamp;
  reason: string | null;
}

export interface ImageClaim {
  key: string;
  original_name?: string;
  remark?: string;
}

export interface TakerSummary {
  id: number;
  username: string;
}

export interface TakerPatch {
  username?: string;
  display_name?: string;
  games?: number[];
  grab_limit_dl?: number;
  grab_limit_pw?: number;
  grab_price_min?: Decimal | null;
  grab_price_max?: Decimal | null;
  is_studio?: boolean;
  business_type?: string;
  member_type?: string;
  grab_forbid_self_service?: boolean;
  live_url?: string;
  qq?: string;
  mobile?: string;
  hired_on?: string | null;
  tag?: string;
  remark?: string;
  order_by?: number;
}

export interface Taker extends TakerSummary {
  display_name: string;
  games: number[];
  grab_limit_dl: number | null;
  grab_limit_pw: number | null;
  grab_price_min: Decimal | null;
  grab_price_max: Decimal | null;
  is_studio: boolean | null;
  business_type: string | null;
  member_type: string | null;
  grab_forbid_self_service: boolean | null;
  live_url: string | null;
  qq: string | null;
  mobile: string | null;
  hired_on: string | null;
  tag: string | null;
  remark: string | null;
  order_by: number | null;
  is_active: boolean;
  deleted_at: Timestamp | null;
}

export interface TakerCreate extends TakerPatch {
  username: string;
  games: number[];
}

export interface TakerQuery extends PageQuery {
  q?: string;
  after?: number;
  include_deleted?: boolean;
  is_active?: boolean;
}

export interface HallQuery extends PageQuery {
  source_order_no?: string;
  keyword?: string;
  game?: number;
  server?: number;
  project_text?: string;
  price_min?: Decimal;
  price_max?: Decimal;
}

export interface HallOrder {
  order_no: string;
  source_order_no: string;
  game: number;
  game_name: string;
  server: number | null;
  project_text: string;
  content: string;
  time_requirement: string;
  is_urgent: boolean;
  hall_price: Decimal | null;
  created_at: Timestamp;
}

export interface ClaimedOrder extends HallOrder {
  status: string;
  status_text: string;
  claimed_at: Timestamp | null;
  updated_at: Timestamp;
  customer_phone: string;
  customer_ww: string;
  total_price: Decimal | null;
}

export interface DictionaryEntry {
  id: number;
  name: string;
}

export interface Game extends DictionaryEntry {
  is_active: boolean;
  sort_order: number;
}

export interface GameServer extends Game {
  game_id: number;
}

export interface Project extends DictionaryEntry {
  base_price: Decimal | null;
  category: string;
  sort_order: number;
}

export interface OrderSource extends DictionaryEntry {
  remark: string;
  order_by: number;
}

export type ChangeResource = "order" | "taker";
export type ChangeAction = "created" | "updated" | "deleted" | "restored";

export interface Change {
  event_id: string;
  resource: ChangeResource;
  resource_id: string;
  action: ChangeAction;
  occurred_at: Timestamp;
}

export interface ChangePage {
  results: Change[];
  next_cursor: string;
  has_more: boolean;
}

export type ChangeQuery = {
  cursor?: string;
  start?: never;
  page_size?: number;
} | {
  cursor?: never;
  start: "now";
  page_size?: number;
};

export interface ChangeWebhook extends Change {
  event: "integration.change";
  delivery_id: string;
  timestamp: number;
  sign: string;
}
