# HVCS Form Runner

Local web tool đọc Excel, cho chọn sheet, sinh câu trả lời theo `tutorial-form.docx`, gửi đa luồng vào Google Form public và ghi trạng thái trực tiếp vào cột F của sheet được chọn.

## Chạy tool

```powershell
cd D:\HVCS-GGForm-Tool
python -m pip install -r requirements.txt
python run.py
```

Mở `http://127.0.0.1:8765`.

## Luồng sử dụng

1. Bấm **Chọn file trực tiếp** để mở hộp thoại Windows. Tool sẽ ghi cột F ngay vào file gốc. Đóng workbook trong Excel trước khi chạy.
   - Nếu không muốn sửa file gốc, dùng **Upload bản sao**.
2. Chọn sheet, xem preview nếu cần.
3. Bấm **Đọc schema** để kiểm tra Form và mở preview đủ 21 câu hỏi. Preview hiển thị loại input, option, grid, required, entry ID và rule mà tool sẽ dùng.
4. Chạy **Dry-run** trước. `DRY_RUN_OK | CHƯA GỬI FORM` chỉ xác nhận payload hợp lệ; không POST và không tạo câu trả lời trong Google Form.
5. Chọn tốc độ gửi theo `record/phút`. Ví dụ `10` nghĩa là tool phân bổ đều một lượt gửi mỗi 6 giây; retry cũng chịu giới hạn này.
6. Có thể bật **Random thời gian giữa các record**, mặc định 5–30 giây. Rate limit vẫn là trần an toàn, nên khoảng thực tế là giá trị lớn hơn giữa rate limit và số giây random.
7. Khi payload đã ổn, bỏ chọn Dry-run và xác nhận chạy live.
8. Theo dõi từng record và tải lại workbook đã có cột `FORM_STATUS` ở cột F.

## Quy tắc dữ liệu

- Giá trị Excel được đọc theo chuỗi hiển thị. Ngày Excel được xuất thành `dd/MM/yyyy`.
- SĐT 9 chữ số được thêm `0` ở đầu; SĐT 10 chữ số bắt đầu bằng `0` được giữ nguyên.
- SĐT còn lại được thay bằng số di động Việt Nam sinh deterministic theo record.
- Các câu random cũng deterministic trong cùng một job; retry không đổi câu trả lời.
- Success chỉ được ghi khi HTML trả về chứa câu xác nhận của Google.
- Timeout sau khi đã bắt đầu POST không tự retry để giảm nguy cơ submit trùng.

## Trạng thái và chạy lại

- Không dùng database.
- Tool chỉ process sheet được chọn; các sheet còn lại không được đọc để gửi Form.
- Cột `F1` của sheet được chọn được đặt thành `FORM_STATUS`.
- Các dòng có `SUCCESS` ở cột F được bỏ qua khi chạy lại.
- Dòng lỗi chứa `FAILED | mã lỗi | mô tả lỗi` ngay tại cột F.
- Chế độ chọn file trực tiếp không tạo bản upload; trạng thái được ghi ngay vào đường dẫn đã chọn.
- `data/uploads/` chỉ dùng khi chọn chế độ Upload bản sao.

Không có dữ liệu nào được gửi khi checkbox Dry-run đang bật.
