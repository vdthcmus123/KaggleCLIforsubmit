# Kaggle Public Kernel Submitter

Hệ thống này dùng GitHub Actions để tự động:

1. Quét các notebook công khai của một hoặc nhiều cuộc thi Kaggle.
2. Với cuộc thi notebook-only, lấy danh sách **Hotness** mới nhất từ đầu ở mỗi
   lần chạy.
3. Duyệt từ trên xuống, lấy đúng version tạo ra Public Score của notebook đầu
   tiên chưa từng được xử lý và tài khoản Kaggle có thể tái chạy.
4. Chạy lại notebook trong tài khoản Kaggle của bạn.
5. Kiểm tra file kết quả.
6. Nộp bài và theo dõi trạng thái chấm điểm.
7. Lưu lịch sử để không nộp lại cùng source version hoặc cùng output và không
   vượt quota đã cấu hình.

Hệ thống hỗ trợ cả cuộc thi cho phép tải file trực tiếp và cuộc thi bắt buộc
nộp từ Kaggle Notebook.

> **Lưu ý:** Trước khi bật nộp tự động, hãy đọc kỹ Rules của cuộc thi và bảo
> đảm việc sử dụng notebook công khai được cho phép. Notebook có Public Score
> cao không đồng nghĩa với việc tài khoản khác luôn có thể tái chạy: notebook
> có thể dùng private dataset, input đã bị xóa, quyền truy cập riêng hoặc môi
> trường đã thay đổi.

## Cơ chế an toàn

Một lượt nộp thật chỉ xảy ra khi đồng thời thỏa mãn cả hai điều kiện:

1. Competition có `enabled = true` và `auto_submit = true` trong
   `config.toml`.
2. Chương trình được gọi với `--submit`, hoặc ô `submit` được chọn khi chạy
   workflow thủ công.

Nếu chạy workflow thủ công mà không chọn `submit`, hệ thống chỉ chạy dry-run:
quét và báo notebook dự kiến chọn nhưng không nộp bài.

## Hệ thống vận hành như thế nào

### 1. Kích hoạt workflow

Workflow tại `.github/workflows/kaggle-harvest.yml` được kích hoạt theo hai
cách:

- Tự động lúc **08:00** hằng ngày theo giờ Việt Nam.
- Thủ công bằng nút **Actions -> Kaggle public kernel harvest -> Run workflow**.

GitHub Actions dùng UTC nên lịch tương ứng là:

```yaml
- cron: "0 1 * * *"  # 08:00 Việt Nam
```

GitHub có thể bắt đầu workflow trễ vài phút khi hệ thống đông. Các lượt kích
hoạt của cùng workflow không chạy chồng lên nhau.

### 2. Chuẩn bị môi trường

Mỗi lần chạy, GitHub Actions sẽ:

1. Checkout nhánh mặc định của repository.
2. Cài Python 3.11 và các thư viện trong `requirements.txt`.
3. Chạy toàn bộ unit test.
4. Đọc `config.toml`.
5. Xử lý lần lượt từng khối `[[competitions]]`.
6. Upload log và output dưới dạng GitHub artifact, giữ trong 7 ngày.
7. Commit thay đổi của `state/harvester-state.json` về repository.

Nếu unit test lỗi, bước quét và nộp bài sẽ không chạy.

### 3. Xử lý từng competition

Với mỗi competition:

1. `enabled = false`: bỏ qua hoàn toàn.
2. Lượt nộp thật nhưng `auto_submit = false`: không nộp.
3. Kiểm tra submission đang ở trạng thái pending từ lượt trước.
4. Kiểm tra giới hạn theo lượt và theo ngày.
5. Chạy pipeline tương ứng với loại competition.

Submission thành công, pending và scoring error đều được tính vào giới hạn
`max_submissions_per_day`. Guardrail theo ngày hiện được tính theo ngày UTC.

## Hai chế độ nộp bài

### Chế độ A: Competition cho tải file trực tiếp

Dùng cấu hình:

```toml
code_competition = false
auto_copy_notebook = false
```

Pipeline:

1. Lấy danh sách public kernel theo `sort_by`.
2. Đọc danh sách output của từng kernel từ trên xuống.
3. Tìm file khớp `file_pattern`.
4. Kiểm tra file theo các quy tắc đã cấu hình.
5. Gọi Kaggle API/CLI để tải file trực tiếp lên competition.

Chế độ này phù hợp với các cuộc thi cho phép dùng lệnh
`kaggle competitions submit`.

### Chế độ B: Competition chỉ nhận bài từ Notebook

Dùng cấu hình:

```toml
code_competition = true
auto_copy_notebook = true
kernel_type = "notebook"
```

Pipeline chi tiết:

1. Gọi cùng dịch vụ `ListKernels` mà trang **Competition -> Code** của Kaggle
   sử dụng.
2. Yêu cầu Kaggle sắp xếp notebook thường theo **Hotness**, loại notebook được
   ghim và các dòng chưa có Public Score, rồi giữ nguyên thứ tự trả về.
3. Bắt đầu lại từ hạng Hotness đầu tiên ở mọi workflow; state chỉ dùng để bỏ
   qua nguồn đã xử lý, không dùng để lưu hay tái sử dụng danh sách cũ.
4. Lấy `scriptVersionId` gắn với Public Score tốt nhất của từng notebook.
5. Quy đổi thành nguồn bất biến dạng `owner/slug/version`; không tự ý dùng
   version mới nhất nếu version đó không phải version tạo ra điểm đang xét.
6. Kiểm tra state. Nếu đúng source version hoặc hash output đã từng thành công,
   pending, scoring error hay bị loại, bỏ qua và xét dòng Hotness tiếp theo.
7. Kiểm tra notebook và toàn bộ input có thể truy cập hay không.
8. Nếu nguồn dùng private dataset, input bị khóa hoặc không thể sao chép, ghi
   nhận lý do rồi chuyển xuống notebook tiếp theo.
9. Copy & Edit đúng source, input version, competition data, accelerator và
   thiết lập chạy sang một private notebook thuộc tài khoản của
   `KAGGLE_API_TOKEN`.
10. Tạo alias runtime cho các kiểu đường dẫn input Kaggle khác nhau mà không
   thay đổi thuật toán của notebook.
11. Push và chạy notebook đã copy trên Kaggle.
12. Chờ notebook hoàn tất, tải output về và chọn file đúng
    `submission_file_name` hoặc `file_pattern`.
13. Kiểm tra tên file, header, dữ liệu rỗng và các quy tắc riêng đã cấu hình.
14. Nộp **version notebook thuộc tài khoản của bạn** bằng Kaggle Code
    Submission API.
15. Chờ Kaggle chấm điểm và lưu kết quả vào state.

Nếu notebook cao điểm nhất bị lỗi runtime, thiếu output, sai định dạng hoặc bị
Kaggle scoring error, hệ thống ghi nguồn đó vào `rejected_sources` hoặc
`failed_submissions`, rồi thử notebook khả dụng tiếp theo trong cùng lượt cho
đến khi:

- Có một submission được tạo.
- Hết `max_submit_attempts_per_run`.
- Hết giới hạn nộp của lượt hoặc của ngày.
- Không còn notebook phù hợp trong `top_n`.

Vì vậy, tiêu chí thực tế là **dòng Hotness cao nhất có Public Score, chưa từng
được xử lý và tài khoản có thể tái chạy hợp lệ**, không phải notebook có Public
Score cao nhất toàn trang.

Một `owner/slug/version` chỉ được thử một lần. Nguồn đã thành công, pending,
scoring error hoặc bị loại đều không chạy lại ở lịch sau. Nếu cùng notebook có
một version mới tạo điểm khác, version mới được coi là nguồn mới; hash output
vẫn được kiểm tra để tránh nộp lại đúng nội dung cũ.

### Submission pending

Kaggle có thể nhận bài nhưng chưa chấm xong trong
`submission_wait_timeout_seconds`. Khi đó:

1. Workflow lưu `submission_ref` vào `pending_submissions`.
2. Workflow kết thúc thành công thay vì coi timeout là scoring error.
3. Lượt kế tiếp kiểm tra lại chính submission đó trước.
4. Nếu vẫn pending, lượt mới không tạo thêm submission cho competition này.
5. Nếu đã hoàn tất hoặc thất bại, lượt đó tiếp tục quét notebook theo thứ tự
   điểm.

Không xóa bản ghi pending khỏi state khi Kaggle chưa trả trạng thái cuối.

## Cấu trúc repository

| Đường dẫn | Chức năng |
| --- | --- |
| `scripts/kaggle_harvest.py` | Chương trình quét, copy, chạy, kiểm tra và nộp bài |
| `config.toml` | Cấu hình thật đang được workflow sử dụng |
| `config.example.toml` | Mẫu cấu hình cho competition mới |
| `.github/workflows/kaggle-harvest.yml` | Lịch chạy và các bước GitHub Actions |
| `state/harvester-state.json` | Lịch sử submission, pending, scoring error và nguồn bị loại |
| `tests/test_kaggle_harvest.py` | Unit test của pipeline |
| `work/` | Source notebook tạm thời, không commit |
| `submissions/` | Output tải từ Kaggle, không commit |
| `logs/` | Log từng lượt, không commit; được upload thành artifact |

## Cài đặt lần đầu

### Yêu cầu

- Python 3.11 trở lên.
- Tài khoản Kaggle đã xác minh theo yêu cầu của competition.
- Đã tham gia competition và chấp nhận Rules.
- Kaggle API token còn hiệu lực.
- Repository GitHub nên để private vì đây là hệ thống tự động dùng credential.

### Chạy ở máy local

Cài thư viện:

```bash
python -m pip install -r requirements.txt
```

Đăng nhập Kaggle:

```bash
kaggle auth login
```

Hoặc đặt biến môi trường `KAGGLE_API_TOKEN` bằng token tạo trong phần Kaggle
API settings.

Nếu chưa có `config.toml`, tạo từ file mẫu:

```powershell
Copy-Item config.example.toml config.toml
```

Chạy test:

```bash
python -m unittest discover -s tests -v
```

Chạy dry-run:

```bash
python scripts/kaggle_harvest.py --config config.toml
```

Chạy nộp thật:

```bash
python scripts/kaggle_harvest.py --config config.toml --submit
```

Lệnh nộp thật vẫn không nộp competition có `auto_submit = false`.

### Thiết lập GitHub Actions

1. Push toàn bộ repository lên một private GitHub repository.
2. Trên Kaggle, vào phần API settings và tạo token mới.
3. Trên GitHub, mở:
   **Settings -> Secrets and variables -> Actions -> New repository secret**.
4. Tạo secret có tên chính xác `KAGGLE_API_TOKEN`.
5. Dán giá trị token Kaggle vào secret.
6. Bảo đảm `config.toml` đã được commit trên nhánh mặc định.
7. Mở tab **Actions** và cho phép Actions nếu repository đang tắt tính năng
   này.

Không commit token Kaggle vào source code, `config.toml`, log hoặc state.

## Cấu hình `config.toml`

`[defaults]` chứa giá trị mặc định dùng chung. Mỗi `[[competitions]]` là một
competition độc lập và có thể ghi đè các giá trị mặc định.

### Các trường chính

| Trường | Ý nghĩa |
| --- | --- |
| `slug` | Slug trong URL của competition |
| `enabled` | Cho phép hệ thống xử lý competition |
| `auto_submit` | Cho phép nộp thật khi workflow chạy với `--submit` |
| `top_n` | Số notebook tối đa lấy từ Kaggle để xét |
| `sort_by` | `hotness` cho chế độ tự Copy & Edit; upload-file vẫn có thể dùng `scoreDescending` hoặc `scoreAscending` |
| `language` | Lọc ngôn ngữ notebook, thường là `python` |
| `kernel_type` | `notebook`, `script` hoặc `all` |
| `output_type` | Bộ lọc output của Kaggle kernel list |
| `file_pattern` | Regex chọn file submission |
| `submission_file_name` | Tên output bắt buộc; không đổi tên file khác để lách điều kiện |
| `code_competition` | `true` nếu Kaggle chỉ nhận submission từ Notebook |
| `auto_copy_notebook` | Tự Copy & Edit, chạy và nộp notebook công khai |
| `require_public_score` | Chỉ coi submission thành công khi Kaggle trả Public Score |
| `required_columns` | Danh sách cột CSV bắt buộc, theo đúng thứ tự |
| `dataset_shapes` | Dataset bắt buộc và giới hạn tọa độ `[t, z, y, x]` |
| `max_submissions_per_run` | Số submission tối đa trong một workflow |
| `max_submit_attempts_per_run` | Số notebook tối đa được thực sự khởi chạy trong một workflow |
| `max_submissions_per_day` | Guardrail số lần nộp mỗi ngày cho competition |
| `notebook_wait_timeout_seconds` | Thời gian tối đa chờ một notebook chạy |
| `notebook_poll_seconds` | Chu kỳ kiểm tra trạng thái notebook |
| `submission_wait_timeout_seconds` | Thời gian chờ Kaggle chấm trước khi chuyển sang pending |
| `submission_poll_seconds` | Chu kỳ kiểm tra trạng thái submission |
| `source_kernel_versions` | Ép version cho một source đặc biệt; thường để `{}` |
| `dataset_source_versions` | Ép version dataset đặc biệt; thường để `{}` |
| `copy_slug_prefix` | Tiền tố notebook riêng do hệ thống tạo |
| `message_template` | Nội dung mô tả submission |

`source_kernel_versions` và `dataset_source_versions` chỉ nên dùng khi cần
override thủ công một trường hợp đã kiểm tra. Bình thường hãy để `{}` để hệ
thống dùng đúng version gắn với Public Score và metadata của notebook nguồn.

## Thêm một competition mới

### Bước 1: Tham gia và xác định loại nộp

1. Mở trang competition bằng đúng tài khoản của API token.
2. Join competition và chấp nhận Rules.
3. Kiểm tra competition cho upload file trực tiếp hay chỉ nhận từ Notebook.
4. Kiểm tra metric: điểm cao hơn tốt hơn hay điểm thấp hơn tốt hơn.
5. Đọc định dạng submission mẫu, tên file, tên cột và giới hạn mỗi ngày.

### Bước 2: Thêm khối cấu hình

Không xóa các competition đang hoạt động. Thêm một khối
`[[competitions]]` mới vào cuối `config.toml`.

#### Mẫu cho competition upload file trực tiếp

```toml
[[competitions]]
slug = "your-competition-slug"
enabled = true
auto_submit = false
top_n = 100
sort_by = "scoreDescending"
language = "python"
kernel_type = "all"
output_type = "data"
file_pattern = "(?i).*submission.*\\.csv$"
code_competition = false
auto_copy_notebook = false
max_submissions_per_run = 1
max_submit_attempts_per_run = 1
max_submissions_per_day = 2
message_template = "AUTO public kernel {kernel_ref} file {file_name}"
```

#### Mẫu cho competition chỉ nhận từ Notebook

```toml
[[competitions]]
slug = "your-competition-slug"
enabled = true
auto_submit = false
top_n = 100
sort_by = "hotness"
language = "python"
kernel_type = "notebook"
output_type = "data"
file_pattern = "(?i).*submission.*\\.csv$"
submission_file_name = "submission.csv"
code_competition = true
auto_copy_notebook = true
copy_slug_prefix = "auto"
notebook_wait_timeout_seconds = 7200
notebook_poll_seconds = 60
submission_wait_timeout_seconds = 1800
submission_poll_seconds = 15
require_public_score = true
source_kernel_versions = {}
dataset_source_versions = {}
max_submissions_per_run = 1
max_submit_attempts_per_run = 3
max_submissions_per_day = 2
message_template = "AUTO public notebook {kernel_ref} file {file_name}"
```

Điều chỉnh timeout theo thời gian chạy thực tế của notebook. Tổng thời gian
workflow hiện bị giới hạn bởi `timeout-minutes: 180`.

### Bước 3: Thêm kiểm tra định dạng nếu cần

Ví dụ competition yêu cầu đúng bốn cột:

```toml
required_columns = [
  "id",
  "prediction",
  "confidence",
  "fold",
]
```

Nếu competition dùng dữ liệu không gian và cần kiểm tra mọi test dataset:

```toml
[competitions.dataset_shapes]
"test_set_a" = [100, 64, 256, 256]
"test_set_b" = [80, 32, 128, 128]
```

Khối `[competitions.dataset_shapes]` thuộc về `[[competitions]]` gần nhất phía
trên nó. Không sao chép cấu hình shape của competition cũ sang competition mới
nếu định dạng không giống nhau.

Với file `.zip`, sửa `file_pattern`, ví dụ:

```toml
file_pattern = "(?i).*submission.*\\.zip$"
```

Đồng thời bỏ `required_columns` và `dataset_shapes` nếu chúng chỉ áp dụng cho
CSV.

### Bước 4: Kiểm thử ở chế độ không nộp

Giữ:

```toml
enabled = true
auto_submit = false
```

Sau đó chạy:

```bash
python -m unittest discover -s tests -v
python scripts/kaggle_harvest.py --config config.toml
```

Commit và push thay đổi, rồi chạy workflow thủ công mà **không chọn ô
submit**. Trong summary cần thấy đúng competition, đúng thứ tự điểm và đúng
source dự kiến.

### Bước 5: Bật nộp thật

Sau khi dry-run đúng:

1. Đổi `auto_submit = true` cho competition mới.
2. Commit và push.
3. Chạy workflow thủ công một lần với ô `submit` được chọn.
4. Kiểm tra cả GitHub Actions summary và tab Submissions trên Kaggle.
5. Chỉ giữ `auto_submit = true` khi submission đầu tiên hợp lệ.

Từ lượt kế tiếp, competition mới tự tham gia lịch 08:00. Không cần
tạo thêm workflow nếu tất cả competition dùng cùng lịch.

## Cấu hình Biohub hiện tại

Repository này đã có cấu hình cho:

```toml
slug = "biohub-cell-tracking-during-development"
code_competition = true
auto_copy_notebook = true
sort_by = "hotness"
submission_file_name = "submission.csv"
max_submissions_per_run = 1
max_submit_attempts_per_run = 3
max_submissions_per_day = 5
```

Biohub yêu cầu đúng header:

```text
id,dataset,row_type,node_id,t,z,y,x,source_id,target_id
```

`dataset_shapes` trong `config.toml` yêu cầu đủ bốn test dataset đã biết và
kiểm tra giới hạn tọa độ. Với source đúng version gắn với Public Score, hệ
thống giữ nguyên giá trị output gốc nhưng vẫn yêu cầu file không rỗng, đúng
header và có đủ dataset đã cấu hình.

## Dừng và xóa competition cũ

Nên thực hiện theo hai giai đoạn để không bỏ sót submission đang pending.

### Giai đoạn 1: Dừng ngay lập tức

1. Kiểm tra tab GitHub Actions, bảo đảm không có workflow của competition đang
   chạy.
2. Kiểm tra tab Kaggle Submissions, bảo đảm không còn bài đang pending.
3. Trong đúng khối `[[competitions]]`, đổi:

```toml
enabled = false
auto_submit = false
```

4. Commit và push.

Từ thời điểm đó, các lịch sau vẫn có thể chạy cho competition khác nhưng sẽ bỏ
qua competition đã tắt.

### Giai đoạn 2: Xóa khỏi cấu hình

Khi competition đã kết thúc và không còn submission pending:

1. Xóa toàn bộ khối `[[competitions]]` có `slug` tương ứng trong
   `config.toml`.
2. Xóa cả khối con `[competitions.dataset_shapes]` của competition đó, nếu có.
3. Không xóa hoặc sửa khối của competition khác.
4. Chạy test và dry-run.
5. Commit và push.

Ví dụ, cần xóa trọn khối sau chứ không chỉ xóa dòng `slug`:

```toml
[[competitions]]
slug = "old-competition"
enabled = false
auto_submit = false
# ...toàn bộ cấu hình còn lại...

[competitions.dataset_shapes]
"old_test_set" = [100, 64, 256, 256]
```

### Có cần xóa state không?

Không bắt buộc. Các bản ghi trong `state/harvester-state.json` đều gắn với
`competition`, nên lịch sử của competition cũ không ảnh hưởng competition
khác. Giữ lại state là lựa chọn an toàn và có lợi cho việc kiểm tra sau này.

Nếu cần xóa sạch lịch sử, chỉ xóa các object có:

```json
"competition": "old-competition"
```

khỏi bốn mảng:

- `submissions`
- `pending_submissions`
- `failed_submissions`
- `rejected_sources`

Không xóa toàn bộ file state khi vẫn còn competition khác. Không xóa bản ghi
pending trước khi Kaggle trả trạng thái cuối. Sau khi sửa, phải bảo đảm file
vẫn là JSON hợp lệ.

### Dọn tài nguyên tùy chọn

Sau khi xác nhận không còn submission liên quan:

- Có thể xóa các private notebook do hệ thống tạo trong tài khoản Kaggle.
- Có thể xóa thư mục local `work/`, `submissions/` và `logs/`.
- GitHub artifacts tự hết hạn sau 7 ngày.
- Không xóa GitHub secret `KAGGLE_API_TOKEN` nếu competition khác còn dùng.

Nếu đã xóa competition cuối cùng:

1. Tắt hoặc xóa phần `schedule` trong workflow để tránh hai lượt chạy rỗng mỗi
   ngày.
2. Có thể xóa secret `KAGGLE_API_TOKEN`.
3. Có thể archive repository nếu không dùng lại.

## Đọc kết quả GitHub Actions

Các cột chính trong **Kaggle harvest summary**:

| Cột | Ý nghĩa |
| --- | --- |
| `Kernels` | Số kernel/notebook Kaggle trả về để xét |
| `Candidates` | Số nguồn đã được phân tích |
| `Submitted` | Số bài đã chấm thành công trong lượt |
| `Pending` | Bài Kaggle đã nhận nhưng chưa có kết quả cuối |
| `Errors` | Lỗi API, runtime, output hoặc scoring |

Một job có dấu xanh chưa chắc đã tạo submission. Hãy đọc thêm:

- `Mode: dry-run` hay `Mode: submit`.
- Mảng `submitted`.
- Mảng `pending_submissions`.
- Mảng `failed_submissions`.
- Mảng `rejected_sources`.
- Mảng `skipped`.

Ví dụ `Submitted = 0` có thể là đúng nếu competition bị tắt, `auto_submit` là
false, còn submission pending, đã hết quota hoặc không có notebook khả dụng.

## Lưu ý vận hành

- `hotness` áp dụng cho pipeline Copy & Edit notebook-only. Với pipeline tải
  output trực tiếp, `scoreDescending` dùng khi metric càng cao càng tốt và
  `scoreAscending` dùng khi metric càng thấp càng tốt.
- `top_n` quá nhỏ có thể khiến hệ thống không tìm được nguồn khả dụng sau khi
  bỏ qua các notebook lỗi.
- Không đặt `max_submissions_per_day` cao hơn giới hạn thực của Kaggle.
- `max_submit_attempts_per_run` là số notebook được khởi chạy, không phải số
  hàng chỉ được đọc metadata.
- Với notebook-only competition, hệ thống phải chạy lại notebook trong tài
  khoản của bạn; không thể nộp trực tiếp session thuộc tài khoản người khác.
- Việc tác giả đã nộp thành công không bảo đảm bản Copy & Edit luôn chạy lại
  được ở thời điểm hiện tại.
- State được commit bởi GitHub Actions. Không chạy nhiều workflow cùng lúc và
  không sửa state trong khi một workflow đang hoạt động.
- Khi thay đổi Rules, format hoặc quota của competition, cập nhật
  `config.toml` trước lịch nộp tiếp theo.
