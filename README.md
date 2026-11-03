# Deploy Oracle A1 Always Free bằng GitHub Actions

Mục tiêu: **một VM.Standard.A1.Flex, 2 OCPU / 12 GB RAM**, tại home region Singapore.
Giữ tài khoản **Always Free**, không nâng PAYG. Boot volume nên đặt **200 GB** trong
stack nếu chưa có ổ khác. Workflow giữ nguyên stack đã lưu; không tự nâng gói hoặc
đổi shape. Không có cấu hình A1 24 GB miễn phí chạy cả tháng cho loại tài khoản này
theo hạn mức Oracle hiện công bố.

**Chưa được triển khai lên GitHub/Oracle.** Code được kiểm thử cục bộ bằng dữ liệu
giả lập; cần làm các bước dưới đây bằng tài khoản của bạn.

## 1. Đặt các file vào repo dự án của bạn

Dùng trong repo chứa phần mềm/hạ tầng mà bạn đang triển khai lên VPS. GitHub yêu cầu
công việc trên hosted runner liên quan đến dự án trong repo. Không dùng Actions làm
máy chủ hoặc bot chạy liên tục độc lập với dự án.
[Điều khoản Actions](https://docs.github.com/en/site-policy/github-terms/github-terms-for-additional-products-and-features#actions)

Giải nén, đưa **nội dung** thư mục này vào root repo, giữ đúng cấu trúc:

```text
.github/workflows/oci-a1.yml
.gitignore
github_runner.py
oci_a1_hunter.py
README.md
```

Commit lên default branch. Không cần bật GitHub Pages/github.io. Trong giao diện
web, thư mục `.github` có thể bị ẩn trên máy; vẫn phải upload đúng đường dẫn workflow.
Không chỉ upload file ZIP vào repo vì GitHub sẽ không tự giải nén workflow.

## 2. Tạo API signing key của Oracle

Trong Oracle Console, mở avatar → **My profile/User settings**, tìm **API keys**.
Nếu chưa thấy, vào Identity & Security → Domains → domain của tài khoản → Users →
user của bạn → API keys. Tên tab có thể khác giữa các giao diện.

1. Chọn **Add API key → Generate API key pair**.
2. Tải **private API key (.pem)** về máy và lưu cẩn thận.
3. Bấm **Add**. Oracle sẽ hiển thị **Configuration file preview**.
4. Giữ lại `user`, `tenancy` và `fingerprint` trong phần preview.

Đây là **API signing key**, khác khóa SSH để đăng nhập VPS. Không gửi nội dung khóa
vào chat, không commit file khóa hoặc config xác thực vào repo.
[Hướng dẫn API key của Oracle](https://docs.oracle.com/en-us/iaas/Content/API/Concepts/apisigningkey.htm)

Tài khoản dùng API key cần quyền đọc stack/log/plan, tạo job Resource Manager và
cấp tài nguyên mà stack khai báo; đọc compartments, instances và volumes; quản lý
topic/subscription Notifications và bucket/objects riêng tư dùng để lưu tiến độ.
Nếu dùng user hạn chế quyền, cấu hình IAM tương ứng; script sẽ dừng khi không đủ quyền.
Workflow không tự tạo quyền IAM.

## 3. Thêm 4 GitHub repository secrets

Repo → **Settings → Secrets and variables → Actions → Secrets → New repository secret**:

| Secret | Nội dung |
| --- | --- |
| `OCI_USER_OCID` | Giá trị `user=...` trong preview, bỏ phần `user=` |
| `OCI_TENANCY_OCID` | Giá trị `tenancy=...`, bỏ phần `tenancy=` |
| `OCI_FINGERPRINT` | Giá trị `fingerprint=...`, bỏ phần `fingerprint=` |
| `OCI_PRIVATE_KEY` | Toàn bộ nội dung file private API key `.pem`, gồm dòng BEGIN/END |

Dùng cặp khóa mới tạo không có passphrase cho workflow này. Nếu khóa của bạn có
passphrase, cần sửa phần config xác thực trước; không bỏ passphrase trên khóa khác
đang dùng. Không cần cung cấp mật khẩu Gmail hoặc private SSH key của VPS.

Stack và email đã điền sẵn trong `oci_a1_hunter.py`:

- Stack: `instance-20261102-0047`, OCID từ cuộc trò chuyện.
- Region: `ap-singapore-1`.
- Email: `manhcuong2112003@gmail.com`.

Nếu bạn muốn dùng stack khác, sửa hằng `STACK` trước khi chạy. Không đổi sang cấu hình
4 OCPU/24 GB khi vẫn giữ tài khoản Always Free.

## 4. Setup email trước

Repo → **Actions → Deploy Oracle A1 Always Free → Run workflow**:

- Chọn default branch.
- `mode`: **setup**.
- Bấm **Run workflow**.

Setup sẽ tạo hoặc tìm lại:

- Một Notifications topic và email subscription cho bạn.
- Một bucket Object Storage **Standard, private**, tên `vincent-a1-state-...` để lưu
  2 file JSON rất nhỏ: trạng thái và khóa chạy trùng. Không public bucket, không bật
  versioning hay lưu artifact/log vào bucket.

Bucket dùng một phần rất nhỏ hạn mức Object Storage miễn phí (tách biệt với 200 GB
boot/block volume). Nếu tài khoản đã dùng hết Object Storage/requests miễn phí,
cần xử lý dung lượng/hạn mức trước. Script không kiểm toán mọi dịch vụ khác trong tài khoản.
[Hạn mức Oracle](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm)

Mở Gmail (kể cả Spam), bấm **Confirm subscription** trong email Oracle.

## 5. Chạy một lượt kiểm tra

Vào **Run workflow** lần nữa, chọn `mode: tick`. Script sẽ:

1. Xác nhận email subscription ACTIVE và gửi mail kiểm tra một lần.
2. Tiếp tục job Apply bạn đã chạy nếu nó vẫn đang hoạt động.
3. Nếu lần trước chỉ lỗi capacity, tạo Plan mới, đợi tối đa khoảng 60 giây.
4. Đọc kế hoạch: đúng một A1 2/12, boot <=200 GB, performance <=10 VPU/GB;
   từ chối thay/xóa instance hoặc thay đổi tài nguyên khác.
5. Kiểm tra A1 và tổng boot/block volumes hiện có trong các compartment của region.
   Nếu cộng tài nguyên mới sẽ vượt 2 OCPU/12 GB hoặc 200 GB thì dừng.
6. Apply đúng plan đã kiểm tra. Không tự tăng cấu hình, không tự dọn/xóa tài nguyên.

Nếu Plan chưa xong trong 60 giây, script kết thúc lượt hiện tại; lượt sau tiếp tục.
Sau khi gửi Apply, job thực thi trên Oracle, runner GitHub kết thúc. Kết quả được
kiểm tra vào lần `tick` sau; có thể bấm Run workflow thủ công để kiểm tra sớm.

Nếu thiếu quyền đọc một compartment/volume, script dừng thay vì tự coi tài nguyên
đó bằng 0. Kiểm tra này chỉ xét Compute A1 và boot/block volumes cùng home region,
không phải cam kết mọi dịch vụ hoặc mọi hoạt động khác trong tài khoản sẽ miễn phí.
Không tạo thêm tài nguyên thủ công song song trong lúc kiểm tra/chạy Apply.

**Không chạy đồng thời script PC/Cloud Shell cũ.** Bản GitHub dùng state trên Oracle,
khác state cục bộ của bản cũ. VCN và stack bạn đã tạo vẫn được dùng lại.

## 6. Bật chạy theo lịch sau khi kiểm tra thành công

Repo → **Settings → Secrets and variables → Actions → Variables → New repository variable**:

```text
Name: HUNTER_ENABLED
Value: true
```

Lịch mặc định: **mỗi 45 phút**, theo UTC; mỗi job tối đa **5 phút**. Chỉ chạy
ngắn để kiểm tra/cấp hạ tầng, không giữ runner ngủ chờ giữa các lần săn.
Workflow dùng ba biểu thức cron để giữ khoảng cách 45 phút cả khi qua giờ/ngày:
`23 0-23/3 * * *`, `8,53 1-23/3 * * *`, `38 2-23/3 * * *`.
Ví dụ theo giờ Việt Nam: **07:23 → 08:08 → 08:53 → 09:38 → 10:23**.
Không dùng `*/45` vì cách đó xen kẽ khoảng cách 45 và 15 phút.
Với 31 ngày, lịch này có tối đa khoảng 992 lượt; nếu mỗi lượt đủ 5 phút thì khoảng
4.960 phút Linux. Đây là ước tính, không phải cam kết hóa đơn bằng 0; còn phụ thuộc
plan, cách đo sử dụng và các workflow khác của tài khoản.

Với repo riêng, kiểm tra **Billing → Actions** và ngân sách/chặn sử dụng trả phí trước
khi bật lịch. GitHub hiện công bố GitHub Free có $12 included compute/tháng;
runner Linux tiêu chuẩn có mức ví dụ $0.006/phút. Không tăng ngân sách trả phí để săn máy.
[GitHub Actions billing](https://docs.github.com/en/billing/concepts/product-billing/github-actions)

Nếu repo dự án công khai và đủ điều kiện dùng standard hosted runner miễn phí,
có thể đổi cron thành `23,53 * * * *` để thử mỗi 30 phút. Không tạo nhiều repo/tài khoản
để né hạn mức. Lịch GitHub có thể trễ; repo công khai không hoạt động lâu có thể bị
tắt scheduled workflow, xem [schedule](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule).

## Sau khi có máy hoặc gặp lỗi khác

- Apply thành công: lưu trạng thái trước, gửi mail kèm link job, **tự disable workflow**.
- Lỗi không phải capacity: lưu lý do dừng, báo mail rồi tắt workflow để bạn kiểm tra.
- Email tạm lỗi: giữ thông báo chưa gửi trên Oracle để lượt sau gửi lại; không tạo thêm VM.
- Oracle trả `429 TooManyRequests`: tạm hoãn đến lượt quét sau, giữ lịch 45 phút.
  Nếu bị giới hạn khi tạo job, lượt sau tìm job theo tên trước khi thử lại để tránh tạo trùng.
  Lượt bị hoãn có thể vẫn hiện dấu xanh trên GitHub; xem log để phân biệt với tạo máy thành công.
- Phản hồi tạo job không rõ: đã lưu ý định trước khi gửi, lượt sau tìm lại job theo tên;
  nếu chưa xác định được thì dừng thay vì gửi thêm job trùng.
- Runner bị timeout: lease tự hết hạn sau 15 phút, lượt sau tiếp tục từ state đã lưu.
- Có thể nhận mail trùng nếu Oracle nhận Publish nhưng phản hồi bị mất; không làm tăng số VM.

Muốn dừng ngay: đổi `HUNTER_ENABLED` thành `false` hoặc chọn **Disable workflow**.
Job Apply đã gửi lên Oracle vẫn có thể chạy tiếp. Không dùng Destroy để dừng việc săn.

Nếu workflow đã tự disable, có thể chọn **Enable workflow** để chạy `status` thủ công.
Nếu state đánh dấu `stopped`, chạy lại `setup` không xóa lỗi; cần review nguyên nhân và
state trước khi cho phép chạy lại. Không xóa bucket/state chỉ để bỏ qua lỗi, đặc biệt
khi chưa kiểm tra máy đã tồn tại hay chưa.

## Giới hạn

- Không bảo đảm lấy được máy; capacity do Oracle quyết định.
- Script chỉ săn cấu hình hiện tại của stack trong hạn mức free, không tìm các shape
  trả phí hoặc chương trình ưu đãi ngoài Always Free.
- Để tận dụng tối đa 200 GB, đặt đúng boot volume trong stack trước lần chạy đầu.
  Script không tự viết lại Terraform khi chỉ thấy cấu hình ổ nhỏ hơn.
- Email xác nhận Apply thành công, không chứng minh ứng dụng hay SSH đã hoạt động.
- Đừng cấp quyền sửa repo/workflow cho người không tin cậy vì workflow dùng API key.
