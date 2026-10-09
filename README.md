# Kaggle Public Notebook Submitter

Tự quét public notebook, chạy lại trong tài khoản Kaggle của bạn và nộp output bằng GitHub Actions.

**Cấu hình hiện tại:** `rsna-knee-abnormality-detection`, quét **Hotness**, mỗi ngày một lần lúc **08:00 giờ Việt Nam**. Lịch GitHub có thể chạy trễ; đây không phải cam kết bắt đầu hoặc nộp bài đúng 08:00.

## Hệ thống vận hành

1. Checkout repository, cài thư viện và chạy unit test. Test dùng cấu hình fixture riêng, không phụ thuộc cuộc thi đang bật.
2. Đọc từng khối `[[competitions]]` trong `config.toml`.
3. Kiểm tra các bài còn pending và giới hạn nộp bài.
4. Lấy danh sách **Competition → Code → Hotness** mới từ đầu, bỏ notebook ghim, giữ nguyên thứ tự Kaggle trả về.
5. Duyệt từ trên xuống. Với `require_public_score = true`, bỏ notebook chưa có Public Score.
6. Xác định đúng version/session gắn với Public Score, không tự chuyển sang version mới nhất khi không lấy được source chính xác.
7. Bỏ source version đã nộp, đang chấm hoặc đã có lỗi cuối cùng được ghi trong lịch sử. Version mới của cùng notebook vẫn có thể được chọn.
8. Copy source đầy đủ và các phiên bản input sang notebook riêng của bạn. Định danh input lấy từ metadata đính kèm, không lấy link trong mô tả dataset. Giữ thiết lập accelerator, image và Internet mà API cung cấp; thêm alias khi đường dẫn input khác giữa giao diện web và CLI.
9. Push notebook, chờ chạy xong. Source/input không truy cập được hoặc notebook lỗi thì ghi lý do và xét ứng viên tiếp theo trong giới hạn số lần thử.
10. Nhận diện file output dùng để nộp, tải đúng file đó, không sửa giá trị, không đổi tên, không ép schema hay đuôi CSV.
11. Kiểm tra file tồn tại, không rỗng và chưa từng nộp cùng SHA-256. Nộp từ owned notebook hoặc upload file tùy loại cuộc thi.
12. Chờ Kaggle chấm. Chỉ đánh dấu thành công khi trạng thái hoàn tất và đáp ứng yêu cầu Public Score; timeout lúc pending được lưu để kiểm tra ở lần chạy sau, không nộp lại ngay.
13. Upload log/output thành artifact, lưu lịch sử trong `state/harvester-state.json` và commit về GitHub.

**Hotness không phải thứ tự điểm cao nhất.** Pipeline chọn notebook khả dụng đầu tiên theo Hotness có Public Score và chưa xử lý đúng version. Muốn duyệt theo điểm thì đặt `sort_by = "scoreDescending"`.

## Nhận diện file nộp tự động

Mặc định `file_pattern = "auto"`. Không cần khai báo danh sách cột.

Ưu tiên theo thứ tự:

1. Nếu có `submission_file_name`: dùng đúng đường dẫn output được chỉ định.
2. File có tên `submission` hoặc `submission.<đuôi bất kỳ>`.
3. File có tên chứa `submission`, nếu chỉ có một ứng viên.
4. Đường dẫn output đọc được tĩnh từ các lệnh ghi file trong source, nếu chỉ có một ứng viên phù hợp.
5. Một artifact duy nhất sau khi loại log, source notebook/script và các checkpoint phổ biến.

Ví dụ: `submission.csv`, `submission.zip`, `submission.parquet`, `submission.json`, `submission.bin`, hoặc `final/answer.dat` đều có thể được dùng. Thư mục con được giữ nguyên khi nộp notebook.

Nếu có nhiều file hợp lý, chương trình báo **Ambiguous submission outputs**, không đoán và không tự sửa file. Khi đó thêm:

```toml
submission_file_name = "final/answer.zip"
```

Đây chỉ là tên/đường dẫn, không phải schema. Không nên đặt cố định nếu các notebook khác nhau tạo tên file khác nhau.

Có thể dùng regex thay `auto` khi thật sự cần một bộ lọc riêng:

```toml
file_pattern = "(?i).*prediction.*\\\\.parquet$"
```

Hệ thống hỗ trợ output ở các định dạng khác nhau nhưng không thể bảo đảm một notebook bất kỳ luôn tạo bài hợp lệ. Kaggle vẫn quyết định định dạng hợp lệ của cuộc thi, giới hạn file và điều kiện chấm.

## Thiết lập GitHub và Kaggle

1. Đăng nhập Kaggle, tham gia cuộc thi và chấp nhận Rules. Tự kiểm tra việc sử dụng public notebook có được phép hay không.
2. Tạo Kaggle API token trong phần Settings của tài khoản.
3. Trong GitHub repository, mở **Settings → Secrets and variables → Actions → New repository secret**.
4. Tạo secret `KAGGLE_API_TOKEN` chứa token. Không đưa token vào source hay log.
5. Cho phép workflow ghi lịch sử: **Settings → Actions → General → Workflow permissions → Read and write permissions**.
6. Chỉnh `config.toml` trên nhánh mặc định của repository, bật cuộc thi cần chạy.
7. Đảm bảo GitHub Actions chưa hết phút/budget và tài khoản Kaggle còn accelerator quota.

Notebook công khai được chạy trên Kaggle, không được thực thi trên GitHub runner. Tuy nhiên đó vẫn là code của người khác; chỉ dùng nguồn bạn tin tưởng và phù hợp Rules.

## Đổi sang cuộc thi mới

Lấy slug từ URL. Ví dụ:

`https://www.kaggle.com/competitions/rsna-knee-abnormality-detection`

Slug là `rsna-knee-abnormality-detection`.

Cấu hình dùng chung:

```toml
[defaults]
top_n = 100
sort_by = "hotness"
language = "python"
kernel_type = "notebook"
output_type = "data"
file_pattern = "auto"
auto_copy_notebook = true
require_public_score = true
notebook_wait_timeout_seconds = 2700
notebook_poll_seconds = 60
submission_wait_timeout_seconds = 1800
submission_poll_seconds = 15
max_submissions_per_run = 1
max_submit_attempts_per_run = 3
max_submissions_per_day = 5

[[competitions]]
slug = "rsna-knee-abnormality-detection"
enabled = true
auto_submit = true
code_competition = true
```

Khi chuyển cuộc thi:

1. Tham gia và chấp nhận Rules trên Kaggle trước.
2. Đổi `slug`; hoặc thêm một khối `[[competitions]]` mới.
3. Đặt `code_competition = true` nếu chỉ cho nộp từ Notebook; đặt `false` nếu nhận upload file.
4. Giữ `auto_copy_notebook = true` để cả hai loại cuộc thi đều chạy lại public code trước khi nộp.
5. Giữ `file_pattern = "auto"`. Xóa tên file/regex riêng của cuộc thi cũ nếu có.
6. Xóa các override `source_kernel_versions`, `dataset_source_versions`, `submit_kernel` và `submit_kernel_version` của cuộc thi cũ nếu không còn phù hợp.
7. Đặt giới hạn theo quota của cuộc thi mới. Giới hạn ngày trong hệ thống tính theo UTC; Kaggle là nơi quyết định quota thực tế.
8. Commit lên nhánh mặc định. Chạy thử dry-run, rồi chọn `submit` để thử nộp thật.

Không cần đổi workflow hoặc sửa code kiểm tra cột khi đổi cuộc thi. Test không đọc cấu hình thật nên đổi slug không làm vỡ test.

## Dừng cuộc thi cũ

Đặt `enabled = false` trong khối cuộc thi cũ, hoặc xóa toàn bộ khối đó khỏi `config.toml`, rồi commit.

Không cần xóa workflow hay xóa lịch sử chạy. Lịch sử được tách theo slug nên cuộc thi cũ không ngăn cuộc thi mới.

Nên giữ `state/harvester-state.json` để bảo toàn thông tin chống nộp trùng. Xóa state sẽ khiến các version cũ có thể được nộp lại.

Nếu không còn cuộc thi nào cần chạy, disable workflow trên GitHub để ngừng dùng phút Actions.

## Chạy thử và lịch tự động

**Chạy thử ngay:** GitHub → Actions → Kaggle public kernel harvest → Run workflow → chọn nhánh `main`.

- Không tick `submit`: chỉ quét và báo ứng viên dự kiến, không push/chạy notebook và không nộp.
- Tick `submit`: chạy và nộp thật nếu `enabled = true` và `auto_submit = true`.

Workflow tại `.github/workflows/kaggle-harvest.yml` có lịch:

```yaml
schedule:
  - cron: "0 1 * * *"
```

01:00 UTC tương ứng 08:00 Việt Nam. GitHub có thể trì hoãn lịch đáng kể khi tải cao. Concurrency ngăn các lượt chạy chồng nhau. Xóa lịch sử run không xóa lịch; xóa/disable file workflow thì không còn tự chạy.

## Xem kết quả và lỗi

Mở run mới nhất → job `harvest` → **Run harvester**. Xem phần summary và tải artifact `kaggle-harvest-<run_id>` nếu cần.

| Hiện tượng | Ý nghĩa |
| --- | --- |
| `Run tests` lỗi, harvester bị skip | Chưa có notebook nào được chạy hoặc nộp trong lượt đó. |
| Không có run mới | Kiểm tra workflow trên nhánh mặc định, trạng thái enabled, phút/budget Actions và lịch. |
| `no new eligible notebook found` | Các ứng viên có điểm đã xử lý hoặc không có source mới khả dụng; không nộp trùng. |
| Source/input private, 403 | Tài khoản không truy cập được version/input; cần nguồn khác. |
| Hết GPU quota | Kaggle không khởi chạy được accelerator; chờ quota reset hoặc tự điều chỉnh nguồn/tài nguyên. |
| Notebook `error` | Xem log tail: lỗi thuộc lần tái chạy notebook, không phải mặc định lỗi upload. |
| `Ambiguous submission outputs` | Có nhiều artifact; cần tên output chính xác. |
| Submission pending timeout | Đã tạo submission nhưng Kaggle chưa chấm xong; state giữ pending và kiểm tra lại lần sau. |
| Submission scoring error | Kaggle đã nhận bài nhưng kết quả/định dạng không hợp lệ; đọc lỗi chấm của Kaggle. |

**Không thể hứa điểm tái chạy bằng điểm nguồn.** Exact version và input giúp tăng khả năng tái lập, nhưng randomness, môi trường, phụ thuộc ngoài, hidden-test rerun hoặc metadata nguồn thiếu vẫn có thể tạo khác biệt.

## Chạy cục bộ

Python 3.11 trở lên:

```powershell
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
python scripts/kaggle_harvest.py --config config.toml
python scripts/kaggle_harvest.py --config config.toml --submit
```

Khai báo `KAGGLE_API_TOKEN` trong môi trường hoặc dùng cấu hình đăng nhập Kaggle CLI của bạn. Lệnh có `--submit` có thể tiêu tốn quota và tạo bài nộp thật.
