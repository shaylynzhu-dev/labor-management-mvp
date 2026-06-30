from pathlib import Path
from zipfile import BadZipFile, ZipFile


MAX_XLSX_BYTES = 10 * 1024 * 1024
MAX_XLSX_UNCOMPRESSED_BYTES = 100 * 1024 * 1024
MAX_XLSX_ARCHIVE_ENTRIES = 1000
MAX_XLSX_ROWS = 10000
MAX_XLSX_COLUMNS = 100


def validate_excel_upload(uploaded):
    if not uploaded or not uploaded.filename:
        raise ValueError("请选择要上传的 Excel 文件")
    if Path(uploaded.filename).suffix.lower() != ".xlsx":
        raise ValueError("仅支持 .xlsx 文件")
    stream = uploaded.stream
    stream.seek(0, 2)
    file_size = stream.tell()
    stream.seek(0)
    if file_size > MAX_XLSX_BYTES:
        raise ValueError("Excel 文件不能超过10MB")
    try:
        with ZipFile(stream) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_XLSX_ARCHIVE_ENTRIES:
                raise ValueError("Excel 压缩包文件项过多")
            if sum(entry.file_size for entry in entries) > MAX_XLSX_UNCOMPRESSED_BYTES:
                raise ValueError("Excel 解压后体积超过100MB")
            if any(entry.flag_bits & 0x1 for entry in entries):
                raise ValueError("不支持加密的 Excel 文件")
    except BadZipFile as error:
        raise ValueError("Excel 文件损坏或格式无效") from error
    finally:
        stream.seek(0)


def validate_excel_shape(frame):
    if len(frame.index) > MAX_XLSX_ROWS:
        raise ValueError(f"Excel 数据不能超过{MAX_XLSX_ROWS}行")
    if len(frame.columns) > MAX_XLSX_COLUMNS:
        raise ValueError(f"Excel 字段不能超过{MAX_XLSX_COLUMNS}列")
