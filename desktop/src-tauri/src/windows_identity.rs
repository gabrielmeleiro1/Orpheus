use windows_sys::Win32::UI::Shell::SetCurrentProcessExplicitAppUserModelID;

pub fn set_app_id(identifier: &str) -> Result<(), i32> {
    let identifier: Vec<u16> = identifier.encode_utf16().chain(Some(0)).collect();
    // SAFETY: the configuration identifier is a valid, null-terminated UTF-16
    // string, and the buffer remains alive for the duration of the call.
    let result = unsafe { SetCurrentProcessExplicitAppUserModelID(identifier.as_ptr()) };
    if result < 0 {
        Err(result)
    } else {
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::set_app_id;
    use windows_sys::Win32::System::Com::CoTaskMemFree;
    use windows_sys::Win32::UI::Shell::GetCurrentProcessExplicitAppUserModelID;

    #[test]
    fn windows_receives_the_bundle_identity() {
        let config: serde_json::Value =
            serde_json::from_str(include_str!("../tauri.conf.json")).unwrap();
        let expected = config["identifier"].as_str().unwrap();
        set_app_id(expected).expect("Windows must accept the bundle identifier");

        let mut actual = std::ptr::null_mut();
        // SAFETY: the API writes a null-terminated COM-allocated string on
        // success. Copy it before freeing it with the matching allocator.
        unsafe {
            assert_eq!(GetCurrentProcessExplicitAppUserModelID(&mut actual), 0);
            assert!(!actual.is_null());
            let mut len = 0;
            while *actual.add(len) != 0 {
                len += 1;
            }
            let value = String::from_utf16_lossy(std::slice::from_raw_parts(actual, len));
            CoTaskMemFree(actual.cast());
            assert_eq!(value, expected);
        }
    }
}
